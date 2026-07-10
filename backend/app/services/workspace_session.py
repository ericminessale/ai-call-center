"""
Workspace session liveness + JWT-epoch invalidation.

Replaces the persona-lease staleness machinery (demo_lease.py) for the
per-visitor-workspace model. Visitor JWTs carry ``{persona: true, wsid:
<workspace public_id>, epoch: <n>}``; both verification paths (custom
``verify_token`` and the flask-jwt-extended blocklist loader) call
``workspace_claims_are_stale`` on every request:

  1. Epoch check — ``bump_workspace_epoch`` on explicit release (and future
     reaping) invalidates every JWT minted under a prior epoch. The Phase 0
     unix-minutes floor is ported verbatim: FLUSHDB (the nightly reset)
     wipes the counter, and a bare INCR restarting at 1 would re-validate
     epochs already baked into ~30-day refresh tokens. Time only moves
     forward, so a wiped counter can never repeat an old epoch.
  2. Liveness check — the Redis session key must exist. Unlike the 5-min
     persona lease, a missing key is NOT authoritative death: the workspace
     row is the durable truth (7-day idle expiry), so on a Redis miss we
     rehydrate from the DB (``workspaces.status`` / ``expires_at``). A
     released/expired workspace fails both the rehydrate and the epoch.

Keys (keyed by workspace public_id — the same opaque id the JWT carries):

    ws:epoch:{public_id}     → epoch counter (persists across sessions)
    ws:session:{public_id}   → '1', TTL = workspace TTL (liveness fast path)
    ws:touch:{public_id}     → '1', TTL 60s (rate-limits last_active bumps)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)


def workspace_ttl_seconds() -> int:
    """Workspace idle lifetime. ``WORKSPACE_TTL_DAYS`` (default 7 days),
    clamped to [1 hour, 30 days]. Cookie max-age and the Redis session TTL
    both track this value.
    """
    raw = os.getenv('WORKSPACE_TTL_DAYS', '7').strip()
    try:
        days = float(raw)
    except ValueError:
        days = 7.0
    seconds = int(days * 24 * 3600)
    return max(3600, min(seconds, 30 * 24 * 3600))


def _epoch_key(public_id: str) -> str:
    return f'ws:epoch:{public_id}'


def _session_key(public_id: str) -> str:
    return f'ws:session:{public_id}'


def _touch_key(public_id: str) -> str:
    return f'ws:touch:{public_id}'


def get_workspace_epoch(public_id: str) -> int:
    """Current epoch for this workspace. 0 if Redis is unavailable or the
    workspace has no recorded epoch yet."""
    redis_client = get_redis_client()
    if redis_client is None:
        return 0
    try:
        raw = redis_client.get(_epoch_key(public_id))
        return int(raw) if raw is not None else 0
    except Exception:
        return 0


def bump_workspace_epoch(public_id: str) -> int:
    """Advance + return the workspace epoch (invalidates JWTs minted under
    any earlier epoch). Called on workspace creation and on release/reap.

    Floored to unix-minutes so epochs stay monotonic across FLUSHDB — see
    module docstring (ported from Phase 0's bump_persona_epoch).
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return 0
    key = _epoch_key(public_id)
    try:
        new = int(redis_client.incr(key))
        floor = int(datetime.utcnow().timestamp() // 60)
        if new < floor:
            redis_client.set(key, floor)
            return floor
        return new
    except Exception as exc:
        # A failed bump silently re-admits prior-session JWTs — make it loud.
        logger.error("bump_workspace_epoch failed for %s: %s", public_id, exc)
        return 0


def mark_session_alive(public_id: str) -> None:
    """(Re)assert the liveness fast-path key at the workspace TTL."""
    redis_client = get_redis_client()
    if redis_client is None:
        return
    try:
        redis_client.set(_session_key(public_id), '1', ex=workspace_ttl_seconds())
    except Exception:
        pass


def end_session(public_id: str) -> None:
    redis_client = get_redis_client()
    if redis_client is None:
        return
    try:
        redis_client.delete(_session_key(public_id))
        redis_client.delete(_touch_key(public_id))
    except Exception:
        pass


def workspace_session_alive(public_id: str) -> bool:
    """Fast liveness: Redis key exists → alive. On a miss, rehydrate from
    the workspace row (Redis flushes must not kill live 7-day workspaces).
    """
    if not public_id:
        return False
    redis_client = get_redis_client()
    if redis_client is not None:
        try:
            if redis_client.exists(_session_key(public_id)):
                return True
        except Exception:
            pass
    # Redis miss (flushed, restarted, or unavailable) — the DB row is the
    # durable truth. Rehydrate the fast path on success.
    try:
        from app.models import Workspace
        ws = Workspace.find_by_public_id(public_id)
        if ws is not None and ws.is_live():
            mark_session_alive(public_id)
            return True
    except Exception as exc:
        logger.error("workspace_session_alive: DB rehydrate failed for %s: %s", public_id, exc)
    return False


def workspace_claims_are_stale(payload: dict) -> bool:
    """True if a visitor token's workspace claims no longer hold.

    Mirrors the old persona staleness contract: epoch mismatch OR dead
    session → stale. Caller has already checked ``payload['persona']``.
    """
    wsid = payload.get('wsid')
    if not wsid:
        return True
    if payload.get('epoch', -1) != get_workspace_epoch(wsid):
        return True
    # The workspace ROW must still exist AND be live, even when the Redis
    # session key does: (a) if the nightly reset deletes workspace rows but
    # its Redis flush fails, a bare session-key hit would keep the token
    # "alive" while its wsid no longer resolves — and an unresolvable wsid
    # downstream means g.workspace_id = None, i.e. the auto-filter silently
    # OFF; (b) if release_workspace commits status='expired' but its epoch
    # bump / end_session no-op during a Redis blip, the surviving epoch +
    # session keys would keep released tokens valid for days. Fail closed on
    # both; the 60s resolve cache bounds the worst case. Cheap: one cached
    # lookup per request.
    ws_id, live = _resolve_workspace(wsid)
    if ws_id is None or not live:
        return True
    return not workspace_session_alive(wsid)


def touch_workspace(workspace) -> None:
    """Bump last_active_at / expires_at, at most once per 60s per workspace
    (Redis NX flag keeps the hot path free of DB writes)."""
    redis_client = get_redis_client()
    if redis_client is not None:
        try:
            if not redis_client.set(_touch_key(workspace.public_id), '1', nx=True, ex=60):
                return
        except Exception:
            pass
    try:
        from app import db
        now = datetime.utcnow()
        workspace.last_active_at = now
        if workspace.expires_at is not None:
            workspace.expires_at = now + timedelta(seconds=workspace_ttl_seconds())
        db.session.commit()
    except Exception as exc:
        logger.warning("touch_workspace failed for %s: %s", workspace.public_id, exc)
        # Don't refresh the Redis liveness key when the DB bump failed —
        # doing so would let token verification outlive the row's expires_at
        # for as long as the DB writes keep failing.
        return
    mark_session_alive(workspace.public_id)


# --------------------------------------------------------------------------
# Cached wsid → (workspace int id, liveness) resolution (for g.workspace_id
# and the persona staleness check). public_id/id pairs are immutable;
# liveness can go stale for up to the 60s TTL, which bounds — instead of
# extending to days — the window where a released/deleted workspace's
# tokens still pass (epoch bumps close it instantly on the healthy path).
# --------------------------------------------------------------------------

_WSID_CACHE: dict = {}
_WSID_CACHE_TTL = 60.0
_WSID_CACHE_MAX = 4096


def _resolve_workspace(public_id: str):
    """``(ws_id, is_live)`` for a JWT ``wsid`` claim; ``(None, False)`` for
    unknown ids. Cached (60s) because it runs on every request."""
    if not public_id:
        return None, False
    import time
    now = time.monotonic()
    hit = _WSID_CACHE.get(public_id)
    if hit is not None and hit[2] > now:
        return hit[0], hit[1]
    try:
        from app.models import Workspace
        ws = Workspace.find_by_public_id(public_id)
    except Exception:
        return None, False
    ws_id = ws.id if ws is not None else None
    live = bool(ws is not None and ws.is_live())
    if len(_WSID_CACHE) >= _WSID_CACHE_MAX:
        _WSID_CACHE.clear()
    _WSID_CACHE[public_id] = (ws_id, live, now + _WSID_CACHE_TTL)
    return ws_id, live


def resolve_workspace_id(public_id: str):
    """Map a JWT ``wsid`` claim to the internal workspace integer id.

    Returns None for unknown ids. Liveness is NOT part of this contract —
    real-user tokens scope by id regardless of workspace lifecycle.
    """
    return _resolve_workspace(public_id)[0]


def user_workspace_alive(user_id: int) -> bool:
    """Liveness by user id — the drop-in for demo_verify's old
    ``has_active_lease(persona_id)`` checks. True iff the user belongs to a
    workspace whose session is alive."""
    try:
        from app.models import User
        from app.tenancy import workspace_context
        with workspace_context(None):
            user = User.query.get(int(user_id))
            if user is None or user.workspace_id is None:
                return False
            workspace = user.workspace
            if workspace is None:
                return False
            return workspace_session_alive(workspace.public_id)
    except Exception:
        return False
