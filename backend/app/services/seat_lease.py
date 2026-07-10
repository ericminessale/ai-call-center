"""
Redis-backed leasing of SignalWire subscriber seats (TENANCY_MODE).

Seats (:class:`app.models.SubscriberSeat`) are the scarce resource — a
fixed pool of pre-provisioned SignalWire subscribers sized to max
concurrently-online browsers. Workspaces are cheap and unbounded; a
visitor's user leases a seat only when their browser actually needs WebRTC
(``POST /api/fabric/token``), and the lease auto-expires unless the
workspace heartbeat keeps refreshing it.

Same SETNX + reverse-index + TTL idiom as the old persona lease:

    seat:lease:{seat_id}   → "<user_id>"   (atomic claim, NX EX)
    seat:user:{user_id}    → "<seat_id>"   (reverse index)

On claim the seat's fabric address is mirrored onto
``user.signalwire_address`` — the queue-dispatch hot paths
(queue_service/queue_dispatch/bridge) dial agents through that column and
stay untouched in Phase 1. The seat row remains the authoritative
credential store; the mirror is cleared on release.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)


def _rotate_seat_password(seat) -> Optional[str]:
    """Rotate the seat's SignalWire subscriber password on the platform.

    Called on every fresh claim (never on a refresh): a TTL-abandoned
    lease leaves the previous browser holding a valid subscriber
    token/registration, so without rotation inbound calls to the seat
    address would still reach the PRIOR workspace's browser. Rotating on
    acquisition kills those stale registrations.

    Returns the new plaintext password on success (caller persists it via
    ``seat.set_subscriber_password``), or None when the platform update
    failed/was skipped — the old password is then still the valid one on
    both sides, so the caller must NOT overwrite the stored credential.
    """
    from app.services import signalwire_client as sw_client

    if not seat.signalwire_subscriber_id or not sw_client.is_configured():
        return None
    candidate = secrets.token_urlsafe(32)
    try:
        sw_client.get_client().fabric.subscribers.update(
            seat.signalwire_subscriber_id, password=candidate,
        )
        return candidate
    except Exception as exc:
        logger.warning(
            "seat_lease: password rotation failed for seat %s (stale "
            "registrations from a prior holder may persist this lease): %s",
            seat.id, exc,
        )
        return None


def _seat_ttl_seconds() -> int:
    """Seat lease idle timeout — refreshed by the workspace heartbeat.
    ``SEAT_LEASE_TTL_SECONDS`` overrides; falls back to the old
    ``DEMO_LEASE_TTL_SECONDS`` knob, then 300s. Clamped to [60, 3600]."""
    raw = (
        os.getenv('SEAT_LEASE_TTL_SECONDS')
        or os.getenv('DEMO_LEASE_TTL_SECONDS')
        or '300'
    ).strip()
    try:
        n = int(raw)
    except ValueError:
        n = 300
    return max(60, min(n, 3600))


def _seat_key(seat_id: int) -> str:
    return f'seat:lease:{int(seat_id)}'


def _user_key(user_id: int) -> str:
    return f'seat:user:{int(user_id)}'


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


def acquire_seat_for_user(user):
    """Return the user's current seat, or atomically claim a free one.

    Returns the :class:`SubscriberSeat` row, or None when every
    credentialed seat is leased (caller surfaces "all seats busy").
    """
    from app import db
    from app.models import SubscriberSeat

    redis_client = get_redis_client()
    if redis_client is None:
        logger.error("seat_lease: Redis unavailable, cannot lease seats")
        return None

    ttl = _seat_ttl_seconds()

    # Already holding a seat? Verify both sides agree, refresh, return.
    raw_seat_id = redis_client.get(_user_key(user.id))
    if raw_seat_id is not None:
        try:
            seat_id = int(_as_str(raw_seat_id))
        except (TypeError, ValueError):
            seat_id = None
        if seat_id is not None:
            holder = redis_client.get(_seat_key(seat_id))
            if holder is not None and _as_str(holder) == str(user.id):
                seat = SubscriberSeat.query.get(seat_id)
                if seat is not None:
                    redis_client.expire(_seat_key(seat_id), ttl)
                    redis_client.expire(_user_key(user.id), ttl)
                    return seat
        # Stale/corrupt reverse index — clear and fall through to a claim.
        redis_client.delete(_user_key(user.id))

    candidates = (
        SubscriberSeat.query
        .filter(SubscriberSeat.signalwire_subscriber_id.isnot(None))
        .order_by(SubscriberSeat.id.asc())
        .all()
    )
    for seat in candidates:
        if not seat.has_credentials():
            continue
        won = redis_client.set(_seat_key(seat.id), str(user.id), nx=True, ex=ttl)
        if not won:
            continue  # someone else holds this one
        redis_client.set(_user_key(user.id), str(seat.id), ex=ttl)
        # Fresh claim → kill the previous holder's SignalWire-side access
        # before this user starts registering with the seat.
        rotated_password = _rotate_seat_password(seat)
        # Informational DB mirror + the dispatch-path address mirror.
        try:
            from app.models import User
            from app.tenancy import workspace_context

            now = datetime.utcnow()
            seat.leased_by_user_id = user.id
            seat.leased_at = now
            seat.lease_expires_at = now + timedelta(seconds=ttl)
            # A TTL-abandoned lease never cleared the previous holder's
            # address mirror (only explicit release does). Steal it here or
            # queue dispatch could still dial the PREVIOUS workspace's user
            # row at this address — which now rings THIS visitor's browser.
            if seat.signalwire_address:
                with workspace_context(None):
                    (
                        User.query
                        .filter(User.signalwire_address == seat.signalwire_address)
                        .filter(User.id != user.id)
                        .filter(User.workspace_id.isnot(None))
                        .update({'signalwire_address': None}, synchronize_session=False)
                    )
            user.signalwire_address = seat.signalwire_address
            if rotated_password:
                seat.set_subscriber_password(rotated_password)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            if rotated_password:
                # The platform already has the NEW password; re-set it on
                # the (rollback-expired) instance so this request's token
                # mint still uses the credential SignalWire now expects.
                # The stored value stays stale until the next successful
                # claim re-rotates — logged loud for that reason.
                seat.set_subscriber_password(rotated_password)
                logger.error(
                    "seat_lease: rotated password for seat %s could not be "
                    "persisted — DB credential is stale until next rotation: %s",
                    seat.id, exc,
                )
            else:
                logger.warning("seat_lease: DB mirror failed for seat %s: %s", seat.id, exc)
        logger.info("seat_lease: seat %s leased to user %s", seat.id, user.id)
        return seat

    logger.warning("seat_lease: pool exhausted — every credentialed seat is leased")
    return None


def heartbeat_seat_for_user(user_id: int) -> bool:
    """Refresh the user's seat lease TTLs. False when no seat is held."""
    redis_client = get_redis_client()
    if redis_client is None:
        return False
    raw_seat_id = redis_client.get(_user_key(user_id))
    if raw_seat_id is None:
        return False
    try:
        seat_id = int(_as_str(raw_seat_id))
    except (TypeError, ValueError):
        return False
    ttl = _seat_ttl_seconds()
    redis_client.expire(_seat_key(seat_id), ttl)
    redis_client.expire(_user_key(user_id), ttl)
    return True


def release_seat_for_user(user_id: int) -> bool:
    """Release the user's seat (workspace release / reset). Idempotent."""
    from app import db
    from app.models import SubscriberSeat, User
    from app.tenancy import workspace_context

    redis_client = get_redis_client()
    if redis_client is None:
        return False
    raw_seat_id = redis_client.get(_user_key(user_id))
    redis_client.delete(_user_key(user_id))
    if raw_seat_id is None:
        return False
    try:
        seat_id = int(_as_str(raw_seat_id))
    except (TypeError, ValueError):
        return False
    redis_client.delete(_seat_key(seat_id))
    try:
        with workspace_context(None):
            seat = SubscriberSeat.query.get(seat_id)
            if seat is not None and seat.leased_by_user_id == int(user_id):
                seat.leased_by_user_id = None
                seat.leased_at = None
                seat.lease_expires_at = None
            user = User.query.get(int(user_id))
            if user is not None and user.workspace_id is not None:
                user.signalwire_address = None
            db.session.commit()
    except Exception as exc:
        from app import db as _db
        _db.session.rollback()
        logger.warning("seat_lease: DB clear failed for seat %s: %s", seat_id, exc)
    logger.info("seat_lease: seat %s released from user %s", seat_id, user_id)
    return True
