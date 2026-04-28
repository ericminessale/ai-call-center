"""
Lease management for the hosted-demo subscriber pool.

Each visitor gets an anonymous session cookie carrying a UUID. The
backend holds a Redis-backed lease that maps that session UUID to one
demo persona (User row with ``role='demo_agent'``). While the lease is
held nobody else can be assigned that persona — defensive invariant
that sidesteps any concurrent-token semantics on the SignalWire side.

Storage layout in Redis (TTL is the source of truth for liveness):

    demo:lease:user:<user_id>    → {"session_token": ..., "leased_at": ...}
    demo:lease:session:<token>   → "<user_id>"

Both keys share a TTL (``LEASE_TTL_SECONDS``, default 300). Heartbeat
refreshes both. Release deletes both. If a visitor closes their tab
without releasing, the lease auto-expires after the TTL elapses and the
persona returns to the free pool.

Invariant: a persona can only be in one of {leased, free}. We enforce
this with ``SET ... NX`` (atomic create-only) on the user-side key, so
two concurrent ``lease_persona()`` calls can't both win the same slot.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from app.models import User
from app.services.redis_service import get_redis_client
from app.utils.demo_config import DEMO_AGENT_ROLE

logger = logging.getLogger(__name__)


def _lease_ttl_seconds() -> int:
    """Lease idle timeout. Heartbeat must arrive within this window or
    the lease auto-releases. Override via ``DEMO_LEASE_TTL_SECONDS``;
    default 300 (5 minutes) — long enough to ride out a network blip,
    short enough to recycle abandoned tabs quickly.
    """
    raw = os.getenv('DEMO_LEASE_TTL_SECONDS', '300').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 300
    return max(60, min(n, 3600))


def _user_key(user_id: int) -> str:
    return f'demo:lease:user:{user_id}'


def _session_key(session_token: str) -> str:
    return f'demo:lease:session:{session_token}'


def get_lease_for_session(session_token: str) -> Optional[User]:
    """Return the User this session currently holds, or None.

    Read-only. Does not refresh the TTL — call :func:`heartbeat_lease`
    explicitly when you want to refresh.
    """
    if not session_token:
        return None
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    raw = redis_client.get(_session_key(session_token))
    if raw is None:
        return None
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None
    return User.query.get(user_id)


def lease_persona(session_token: str) -> Optional[User]:
    """Find or grant a demo-persona lease for this session.

    Idempotent — if the session already holds a lease, refreshes its
    TTL and returns the same persona. If not, walks the pool of
    demo-agent Users and atomically claims the first free one.

    Returns ``None`` when the pool is exhausted (every persona is
    currently leased by someone else) — caller should surface a clear
    "demo full" error to the visitor.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        logger.error("lease_persona: Redis unavailable, cannot manage leases")
        return None

    # Already leased? refresh + return.
    existing = get_lease_for_session(session_token)
    if existing is not None:
        heartbeat_lease(session_token)
        return existing

    # Walk the pool deterministically (by id ASC) and try to claim. The
    # SETNX (set-if-not-exists) is what makes this race-safe across
    # gunicorn workers — only one worker can win for any given user_id.
    ttl = _lease_ttl_seconds()
    leased_at = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    payload = json.dumps({'session_token': session_token, 'leased_at': leased_at})

    candidates = (
        User.query
        .filter_by(role=DEMO_AGENT_ROLE, is_active=True)
        .order_by(User.id.asc())
        .all()
    )
    for user in candidates:
        # Subscriber must exist for the persona to be useful (WebRTC
        # token minting needs it). Skip un-provisioned personas; they
        # contribute nothing to the demo.
        if not user.signalwire_subscriber_id:
            continue
        won = redis_client.set(_user_key(user.id), payload, nx=True, ex=ttl)
        if not won:
            continue  # someone else holds this one
        # Mirror the reverse index so we can look up by session.
        redis_client.set(_session_key(session_token), str(user.id), ex=ttl)
        logger.info(
            "demo_lease: granted persona %s (id=%s) to session %s",
            user.email, user.id, session_token[:8],
        )
        return user

    logger.warning("demo_lease: pool exhausted, no free demo personas")
    return None


def heartbeat_lease(session_token: str) -> bool:
    """Refresh the TTL on a session's lease. Returns ``True`` if the
    lease is still active (TTL was extended), ``False`` if the session
    has no lease (expired or never had one).

    Frontend calls this on a regular interval (~30s) to keep the lease
    alive while the tab is open.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return False
    user_id_raw = redis_client.get(_session_key(session_token))
    if user_id_raw is None:
        return False
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return False
    ttl = _lease_ttl_seconds()
    redis_client.expire(_session_key(session_token), ttl)
    redis_client.expire(_user_key(user_id), ttl)
    return True


def release_lease(session_token: str) -> bool:
    """Release a session's lease, returning the persona to the pool.

    Idempotent — releasing a non-existent lease is a no-op. Returns
    True if a lease was actually released, False if there was nothing
    to release. Either is fine.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return False
    session_k = _session_key(session_token)
    user_id_raw = redis_client.get(session_k)
    if user_id_raw is None:
        return False
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        redis_client.delete(session_k)  # at least clear the corrupted side
        return False
    redis_client.delete(_user_key(user_id))
    redis_client.delete(session_k)
    logger.info(
        "demo_lease: released persona id=%s (session %s)",
        user_id, session_token[:8],
    )
    return True


def count_active_leases() -> int:
    """How many demo personas are currently leased. Telemetry/admin.

    O(N) scan over the pool size — fine for N≤100. If the pool grows
    past that, switch to a Redis SET tracking active user IDs.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return 0
    user_ids = [
        u.id for u in
        User.query.filter_by(role=DEMO_AGENT_ROLE, is_active=True).all()
    ]
    if not user_ids:
        return 0
    keys = [_user_key(uid) for uid in user_ids]
    # MGET-ish: use exists() to count.
    return sum(1 for k in keys if redis_client.exists(k))
