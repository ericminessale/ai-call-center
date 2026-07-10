"""
Daily-reset logic for the hosted demo deployment.

Phase 1 tenancy shape: wipes per-day mutable state (calls, transcriptions,
contacts, queue opt-ins, ratelimit counters, …) AND every visitor
workspace with its cloned resources, while preserving the platform
fixtures — the default/template workspace (id 1) with its queues, KB,
MCP gateway config, and the global (workspace 0) system_config layer.
Finally tops up the subscriber seat pool defensively.

This whole-floor nightly wipe is a Phase 1 stopgap: the Phase 3 reaper
replaces it with per-workspace GC (idle-expiry, no FLUSHDB) so returning
visitors keep their 7-day workspaces. Until then, hosted visitors get a
fresh floor every midnight — same as the persona era.

Triggered by the ``demo-reset`` cron container hitting
``POST /api/internal/demo-reset`` daily at 00:00 UTC. Can also be
called manually for testing. Refuses outside hosted-demo mode — there's
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
    AgentCollectionAssignment,
    Call,
    CallLeg,
    Callback,
    Conference,
    ConferenceParticipant,
    Contact,
    Document,
    DocumentCollection,
    McpGatewayConfig,
    Queue,
    QueueAgentAssignment,
    SubscriberSeat,
    SystemConfig,
    Transcription,
    User,
    WebhookEvent,
    Workspace,
)
from app.tenancy import DEFAULT_WORKSPACE_ID
from app.utils.demo_config import is_demo_mode

logger = logging.getLogger(__name__)


# Tables wiped WHOLESALE in dependency order (children first). These hold
# per-day interaction state in every workspace — including the template
# workspace, which should never accumulate calls anyway.
_WIPE_MODELS_IN_ORDER = (
    Transcription,
    CallLeg,
    ConferenceParticipant,
    Conference,
    WebhookEvent,
    Callback,    # references calls/contacts (SET NULL, but wipe explicitly)
    Call,        # parent of legs/transcriptions
    Contact,     # parent of calls (calls reference contact_id)
    # Queue opt-ins. FLUSHDB empties the queue_agents:{slug} Redis sets;
    # leaving DB rows is_activated=True desyncs checkbox vs dispatch.
    QueueAgentAssignment,
)

# Workspace-cloned resources deleted for every NON-template workspace,
# children first (documents before their collections; assignments before
# collections too).
_WORKSPACE_SCOPED_MODELS_IN_ORDER = (
    Document,
    AgentCollectionAssignment,
    DocumentCollection,
    Queue,
    McpGatewayConfig,
)


def reset_demo_state() -> dict:
    """Run the full nightly reset. Returns a dict suitable for logging.

    Refuses with an explicit error when hosted-demo mode is not set, so
    the cron firing against a production-shape backend is a no-op.
    """
    if not is_demo_mode():
        return {'skipped': 'hosted demo mode not set'}

    db_summary = _wipe_mutable_db_state()
    ws_summary = _wipe_visitor_workspaces()
    redis_summary = _wipe_redis()
    seed_summary = _reseed_defensive()

    return {
        'db': db_summary,
        'workspaces': ws_summary,
        'redis': redis_summary,
        'seed': seed_summary,
    }


def _wipe_mutable_db_state() -> dict[str, Any]:
    """Truncate the mutable-state tables.

    NOTE: ``query(model).delete(synchronize_session=False)`` is a BULK
    delete — it skips ORM mapper events and relationship cascades.
    Correctness rests entirely on _WIPE_MODELS_IN_ORDER being
    children-first; don't add delete-event listeners and expect them to
    fire here. (The tenancy do_orm_execute filter skips non-SELECTs, and
    this runs from the internal cron endpoint with no workspace context
    anyway — the wipe is deliberately install-wide.) Counts are
    best-effort (logged for the operator).
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


def _wipe_visitor_workspaces() -> dict[str, Any]:
    """Delete every visitor workspace: cloned resources, per-workspace
    config layers, visitor users, then the workspace rows themselves.
    The template workspace (id 1) and its resources survive untouched,
    as do platform users (workspace NULL) and the global config layer
    (workspace 0)."""
    counts: dict[str, int] = {}
    try:
        for model in _WORKSPACE_SCOPED_MODELS_IN_ORDER:
            counts[model.__tablename__] = (
                db.session.query(model)
                .filter(model.workspace_id != DEFAULT_WORKSPACE_ID)
                .delete(synchronize_session=False)
            )
        counts['system_config_layers'] = (
            db.session.query(SystemConfig)
            .filter(SystemConfig.workspace_id.notin_(
                (SystemConfig.GLOBAL_WORKSPACE_ID, DEFAULT_WORKSPACE_ID)))
            .delete(synchronize_session=False)
        )
        # Visitor users: clear FK references that survive them, then delete.
        db.session.execute(db.text(
            "UPDATE system_config SET updated_by = NULL WHERE updated_by IN "
            "(SELECT id FROM users WHERE workspace_id IS NOT NULL)"
        ))
        db.session.query(SubscriberSeat).update(
            {'leased_by_user_id': None, 'leased_at': None, 'lease_expires_at': None},
            synchronize_session=False,
        )
        counts['users'] = (
            db.session.query(User)
            .filter(User.workspace_id.isnot(None))
            .delete(synchronize_session=False)
        )
        counts['workspaces'] = (
            db.session.query(Workspace)
            .filter(Workspace.id != DEFAULT_WORKSPACE_ID)
            .delete(synchronize_session=False)
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("demo_reset: workspace wipe failed: %s", exc)
        counts['error'] = str(exc)
    return counts


def _wipe_redis() -> dict[str, Any]:
    """Flush the demo-mode Redis namespace.

    We ``FLUSHDB`` rather than pattern-delete because:
      1. The hosted demo's Redis is dedicated (one container, only
         this app talks to it).
      2. All keys in there are ephemeral demo state — workspace sessions,
         seat leases, queue counters, agent status, ratelimits. Nothing
         valuable to survive a daily wipe. JWT epochs are wiped too, but
         the unix-minutes floor in workspace_session.bump_workspace_epoch
         keeps them monotonic, so no pre-wipe token ever re-validates.
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
    """Re-run the idempotent seat-pool top-up + default-workspace ensure.

    Almost always a no-op (seat rows aren't wiped), but defensive in case
    an operator manually deleted seats. Costs one SignalWire list call
    per missing subscriber at most.
    """
    from app.services.seat_pool import ensure_seat_pool
    from app.services.workspace_provision import ensure_default_workspace

    try:
        ensure_default_workspace()
        return ensure_seat_pool()
    except Exception as exc:
        logger.error("demo_reset: re-seed failed: %s", exc)
        return {'error': str(exc)}
