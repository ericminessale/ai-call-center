"""Stale-call watchdog.

Background sweep that reconciles the DB / Redis queue / Socket.IO state with
reality. Required because SignalWire offers no working callback path for SWML
script lifecycle events (the `set` verb's call_state_url is just a script
variable; the phone-number `call_status_callback_url` field is not honored
for `relay_script` handler types — only for cXML/laml_webhooks).

How it works:
  1. The bridge no-agent SWML hold loop fires a heartbeat ping each iteration
     (every ~20s) via SWML's `request` verb to /api/webhooks/call-heartbeat.
  2. That endpoint refreshes a Redis TTL key ``call_heartbeat:<call_sid>``
     (90s TTL).
  3. This watchdog runs every 15s, scans Call rows with status in
     ``('waiting', 'assigned')``, and for any row whose heartbeat key has
     expired AND is older than the grace period, runs the same end-of-call
     cleanup the /call-status webhook would have run.

Grace period: 60s. Protects against:
  - Brand-new calls that haven't reached their first heartbeat verb yet
  - Transient Redis hiccups that briefly drop the key
  - Slow first-iteration SWML execution after `answer`

Cleanup performed per stale call:
  - call.status -> 'ended', ended_at = now()
  - Remove from any Redis queue zset
  - Release the assigned agent if still marked busy on this call
  - End any active/connecting CallLeg
  - Mark associated Conference ended (cascades to participants)
  - Emit ``queue_update {action: 'ended'}`` so the dashboard removes the row
  - Emit ``call_update`` so the Calls list shows the ended state

The watchdog logs every reap at WARNING level so they're visible in default
gunicorn output — easy to spot if the heartbeat plumbing itself ever breaks.
"""

import logging
from datetime import datetime, timedelta
from typing import List

logger = logging.getLogger(__name__)


# Tunables. Kept module-level so tests / ops can override.
WATCHDOG_INTERVAL_SECONDS = 15
HEARTBEAT_GRACE_SECONDS = 60
ACTIVE_STATUSES = ('waiting', 'assigned')


def _reap_call(call) -> None:
    """Run end-of-call cleanup for one stale call.

    Mirrors the 'ended' branch of /api/webhooks/call-status. Pulled out into
    a helper so any future cleanup callsite (manual admin button, REST API
    sweep) can reuse the same logic and stay in sync.
    """
    from app import db, socketio
    from app.models import Conference, CallLeg
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client

    call_sid = call.signalwire_call_sid
    call.status = 'ended'
    if not call.ended_at:
        call.ended_at = datetime.utcnow()

    redis_client = get_redis_client()
    if redis_client:
        try:
            qs = QueueService(redis_client)
            qs.remove_call_from_all_queues(call_sid)
        except Exception as e:
            logger.warning(f"watchdog reap {call_sid}: dequeue failed: {e}")

        if call.assigned_agent_id:
            try:
                qs = QueueService(redis_client)
                agent_status = qs.get_agent_status(str(call.assigned_agent_id))
                if (
                    agent_status
                    and agent_status.get('status') == 'busy'
                    and agent_status.get('current_call_id') == call_sid
                ):
                    qs.set_agent_status(str(call.assigned_agent_id), 'available')
                    logger.info(
                        f"watchdog reap {call_sid}: released agent "
                        f"{call.assigned_agent_id} (was busy on this call)"
                    )
            except Exception as e:
                logger.warning(f"watchdog reap {call_sid}: agent release failed: {e}")

    # Close active or connecting CallLeg rows.
    try:
        active_leg = CallLeg.get_active_leg(call.id) or db.session.query(CallLeg).filter_by(
            call_id=call.id, status='connecting'
        ).first()
        if active_leg:
            active_leg.end_leg(reason='hangup')
    except Exception as e:
        logger.warning(f"watchdog reap {call_sid}: leg close failed: {e}")

    # Mark conference ended if there is one.
    if call.conference_name:
        try:
            conf = Conference.get_active_by_name(call.conference_name)
            if conf:
                conf.end_conference()
        except Exception as e:
            logger.warning(f"watchdog reap {call_sid}: conference end failed: {e}")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"watchdog reap {call_sid}: commit failed: {e}")
        return

    # Notify the UI.
    try:
        call_data = call.to_dict(include_contact=True)
        socketio.emit('call_update', {'call': call_data})
        socketio.emit('queue_update', {
            'call': call_data,
            'queue_id': call.queue_id,
            'action': 'ended',
        })
        socketio.emit('call_ended', {
            'callId': call.id,
            'call_sid': call_sid,
            'conference_name': call.conference_name,
            'assigned_agent_id': call.assigned_agent_id,
            'reset_ui': True,
        })
    except Exception as e:
        logger.warning(f"watchdog reap {call_sid}: socket emit failed: {e}")

    logger.warning(
        f"[call_watchdog] reaped {call_sid} (status was {call.status!r}, "
        f"queue={call.queue_id}, agent={call.assigned_agent_id})"
    )


def _scan_once(app) -> int:
    """One pass: return count of calls reaped.

    Pushes a fresh app context because this runs in a background greenlet
    that has no implicit context from the request lifecycle.
    """
    from app import db
    from app.models import Call
    from app.services.redis_service import get_redis_client

    reaped = 0
    with app.app_context():
        redis_client = get_redis_client()
        cutoff = datetime.utcnow() - timedelta(seconds=HEARTBEAT_GRACE_SECONDS)

        candidates: List[Call] = (
            db.session.query(Call)
            .filter(Call.status.in_(ACTIVE_STATUSES))
            .filter(Call.created_at < cutoff)
            .all()
        )

        for call in candidates:
            call_sid = call.signalwire_call_sid
            if not call_sid:
                continue

            # Check the heartbeat key. Present → keep. Missing → reap.
            try:
                has_beat = bool(
                    redis_client and redis_client.exists(f"call_heartbeat:{call_sid}")
                )
            except Exception as e:
                logger.warning(f"watchdog scan {call_sid}: exists check failed: {e}")
                continue

            if has_beat:
                continue

            _reap_call(call)
            reaped += 1

    return reaped


def _run_loop(app) -> None:
    """Background loop. Runs forever; logs and continues on any error."""
    from app import socketio
    logger.warning(
        f"[call_watchdog] started (interval={WATCHDOG_INTERVAL_SECONDS}s, "
        f"grace={HEARTBEAT_GRACE_SECONDS}s)"
    )
    while True:
        try:
            n = _scan_once(app)
            if n:
                logger.warning(f"[call_watchdog] swept {n} stale calls this pass")
        except Exception as e:
            # Never let a transient failure kill the loop — we're the safety
            # net of last resort for stale rows; a dead watchdog defeats the
            # purpose.
            logger.error(f"[call_watchdog] scan error: {e}", exc_info=True)
        socketio.sleep(WATCHDOG_INTERVAL_SECONDS)


def start(app) -> None:
    """Spawn the watchdog as a Socket.IO background task.

    Called once at app startup from app.__init__. Idempotent at the module
    level — multiple calls would spawn duplicate workers, so callers must
    only invoke once.
    """
    from app import socketio
    socketio.start_background_task(_run_loop, app)
