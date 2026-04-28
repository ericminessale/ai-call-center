"""
Daily-reset logic for the hosted demo deployment.

Wipes mutable per-day state (calls, transcriptions, contacts, queue
assignments, lease pool, ratelimit counters, etc.) while preserving
fixtures (users, queues, knowledge base, MCP gateway config). Then
runs the idempotent persona seed as a defensive top-up.

Triggered by the ``demo-reset`` cron container hitting
``POST /api/internal/demo-reset`` daily at 00:00 UTC. Can also be
called manually for testing. Refuses outside ``DEMO_MODE`` — there's
no scenario where you'd want to mass-wipe a production-shape DB.

Out of scope (deferred follow-up): resetting the bundled DemoShop
SQLite DB. The customer + product seed is stable; orders accumulate
RMA history across days but bounded enough not to matter for v1.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db, redis_client
from app.models import (
    Call,
    CallLeg,
    Conference,
    ConferenceParticipant,
    Contact,
    Transcription,
    WebhookEvent,
)
from app.utils.demo_config import is_demo_mode

logger = logging.getLogger(__name__)


# Tables wiped in dependency order (children first). Anything not in
# this list is preserved across resets — see module docstring for the
# full classification.
_WIPE_MODELS_IN_ORDER = (
    Transcription,
    CallLeg,
    ConferenceParticipant,
    Conference,
    WebhookEvent,
    Call,        # parent of legs/transcriptions
    Contact,     # parent of calls (calls reference contact_id)
)


def reset_demo_state() -> dict:
    """Run the full nightly reset. Returns a dict suitable for logging.

    Refuses with an explicit error when DEMO_MODE is not set, so the
    cron firing against a production-shape backend is a no-op.
    """
    if not is_demo_mode():
        return {'skipped': 'DEMO_MODE not set'}

    db_summary = _wipe_mutable_db_state()
    redis_summary = _wipe_redis()
    seed_summary = _reseed_defensive()

    return {
        'db': db_summary,
        'redis': redis_summary,
        'seed': seed_summary,
    }


def _wipe_mutable_db_state() -> dict[str, Any]:
    """Truncate the mutable-state tables.

    Uses ORM-level ``delete()`` so SQLAlchemy events fire correctly.
    Counts are best-effort (logged for the operator); they're not used
    to gate anything downstream.
    """
    counts: dict[str, int] = {}
    for model in _WIPE_MODELS_IN_ORDER:
        try:
            n = db.session.query(model).delete(synchronize_session=False)
            counts[model.__tablename__] = n
        except Exception as exc:
            logger.error("demo_reset: wipe of %s failed: %s", model.__tablename__, exc)
            counts[model.__tablename__] = -1
    db.session.commit()
    return counts


def _wipe_redis() -> dict[str, Any]:
    """Flush the demo-mode Redis namespace.

    We ``FLUSHDB`` rather than pattern-delete because:
      1. The hosted demo's Redis is dedicated (one container, only
         this app talks to it).
      2. All keys in there are ephemeral demo state — leases, queue
         counters, agent status, ratelimits. Nothing valuable to
         survive a daily wipe.
      3. Pattern-delete is O(N) per pattern and we'd need at least 6
         patterns; FLUSHDB is one round-trip.
    """
    if redis_client is None:
        logger.warning("demo_reset: redis_client is None, skipping Redis wipe")
        return {'skipped': 'redis_client unavailable'}
    try:
        redis_client.flushdb()
        return {'flushdb': 'ok'}
    except Exception as exc:
        logger.error("demo_reset: FLUSHDB failed: %s", exc)
        return {'flushdb': f'error: {exc}'}


def _reseed_defensive() -> dict[str, Any]:
    """Re-run the idempotent persona seed.

    Almost always a no-op (we don't wipe ``users`` or
    ``mcp_gateway_configs``), but defensive in case an operator
    manually deleted a persona row. Costs nothing.
    """
    from app.services.demo_seed import seed_demo_personas

    try:
        return seed_demo_personas()
    except Exception as exc:
        logger.error("demo_reset: re-seed failed: %s", exc)
        return {'error': str(exc)}
