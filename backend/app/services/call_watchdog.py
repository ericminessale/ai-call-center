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
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)


# Tunables. Kept module-level so tests / ops can override.
WATCHDOG_INTERVAL_SECONDS = 30

# Per-status max age (seconds since created_at) before a non-terminal call is
# considered stale and reaped. This is a SAFETY NET — prompt cleanup happens
# via the queue-status / call-status webhooks (enter_queue fires queue-status
# on hangup/timeout/failed; the call-state webhook handles conference calls).
# These caps only catch the rows those webhooks missed:
#   - SWML scripts that died without a terminal event
#   - frontend optimistic 'active' that never actually bridged (the 44hr-stuck
#     bridge calls that motivated covering 'active' here)
#   - carrier-retry phantoms
# Generous values so we never reap a legitimately-live call.
STALE_MAX_AGE = {
    'pending': 180,    # 3 min  — should promote to 'waiting' quickly
    'waiting': 2100,   # 35 min — just past enter_queue wait_time (1800s)
    'queued': 2100,    # alias of waiting in some paths
    'assigned': 900,   # 15 min — assigned but never went active = stuck dispatch
    'active': 14400,   # 4 hr   — beyond any realistic call-center call duration
}

# Redis lock so only ONE gunicorn worker performs the sweep each interval
# (we run 4 workers, each spawns a watchdog greenlet — without this they all
# reap the same rows simultaneously, producing duplicate log lines + wasted
# work). Best-effort: if Redis is down the lock no-ops and every worker
# sweeps, which is still correct (reap is idempotent), just noisier.
_SWEEP_LOCK_KEY = 'call_watchdog:sweep_lock'


def reap_call(call) -> None:
    """Run end-of-call cleanup for one stale call.

    Mirrors the 'ended' branch of /api/webhooks/call-status. Pulled out into
    a helper so any future cleanup callsite (manual admin button, REST API
    sweep) can reuse the same logic and stay in sync. Public because
    ``queue_dispatch._release_teardown`` is now one of those callsites: the
    hold timeout releases the call deliberately (the caller's own SWML plays
    the promise and hangs up), but the DB/Redis teardown is identical.

    Honours a pre-set ``call.end_reason`` — a caller that already knows how
    the call ended (the hold timeout stamps 'callback_scheduled') keeps its
    classification instead of getting 'abandoned_in_queue' computed over it.
    """
    from app import db, socketio
    from app.models import Conference, CallLeg
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client

    call_sid = call.signalwire_call_sid
    # Classify how it ended BEFORE flipping status to 'ended' (compute reads
    # the pre-end status to decide abandoned_in_queue vs missed etc).
    if not call.end_reason:
        call.end_reason = call.compute_end_reason()
    call.update_status('ended', end_reason=call.end_reason)

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

    # Close ALL active/connecting CallLeg rows (not just the first — multi-leg
    # calls otherwise leave the extras stuck 'active').
    try:
        CallLeg.end_all_open(call.id, reason='hangup')
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

    # F-05: a reaped call is still a finished interaction — finalize its
    # caller memory (stats/digest/index; idempotent, never raises).
    try:
        from app.services.contact_enrichment import finalize_call_memory
        finalize_call_memory(call)
    except Exception as e:
        logger.warning(f"watchdog reap {call_sid}: memory finalization failed: {e}")

    # Notify the call's workspace (§8.1 — reaps were global broadcasts).
    try:
        from app.services.ws_rooms import workspace_room
        room = workspace_room(call.workspace_id)
        call_data = call.to_dict(include_contact=True)
        socketio.emit('call_update', {'call': call_data}, room=room)
        socketio.emit('queue_update', {
            'call': call_data,
            'queue_id': call.queue_id,
            'action': 'ended',
        }, room=room)
        socketio.emit('call_ended', {
            'callId': call.id,
            'call_sid': call_sid,
            'conference_name': call.conference_name,
            'assigned_agent_id': call.assigned_agent_id,
            'reset_ui': True,
        }, room=room)
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
    now = datetime.utcnow()
    with app.app_context():
        redis_client = get_redis_client()

        # Single-worker guard: try to grab the sweep lock for this interval.
        # If another worker holds it, skip this pass entirely.
        if redis_client is not None:
            try:
                got_lock = redis_client.set(
                    _SWEEP_LOCK_KEY, '1', nx=True, ex=WATCHDOG_INTERVAL_SECONDS - 1
                )
                if not got_lock:
                    return 0
            except Exception:
                pass  # Redis hiccup → fall through, every worker sweeps (safe)

        candidates: List[Call] = (
            db.session.query(Call)
            .filter(Call.status.in_(tuple(STALE_MAX_AGE.keys())))
            .all()
        )

        for call in candidates:
            call_sid = call.signalwire_call_sid
            if not call_sid:
                continue

            max_age = STALE_MAX_AGE.get(call.status)
            if max_age is None:
                continue

            created = call.created_at or now
            age = (now - created).total_seconds()
            if age < max_age:
                continue  # not old enough yet — give webhooks time to clean up

            # Fast-skip: if a heartbeat key still exists the SWML script is
            # provably alive (only the legacy goto/label hold loop sets these;
            # harmless if absent). Don't reap a call that's proven live.
            try:
                if redis_client and redis_client.exists(f"call_heartbeat:{call_sid}"):
                    continue
            except Exception:
                pass

            logger.warning(
                f"[call_watchdog] reaping {call_sid}: status={call.status!r} "
                f"age={age:.0f}s exceeds cap {max_age}s"
            )
            reap_call(call)
            reaped += 1

    return reaped


# Redis key for the cross-worker singleton lock (DEPLOY-H4).
_WATCHDOG_LOCK_KEY = 'call_watchdog_lock'
_WATCHDOG_LOCK_TTL = max(30, WATCHDOG_INTERVAL_SECONDS * 3)


def _run_loop(app) -> None:
    """Background loop. Runs forever; logs and continues on any error."""
    from app import socketio
    from app.services.redis_service import get_redis_client
    logger.warning(
        f"[call_watchdog] started (interval={WATCHDOG_INTERVAL_SECONDS}s, "
        f"age caps={STALE_MAX_AGE})"
    )
    while True:
        try:
            # Refresh the singleton lock so it survives while we're alive but
            # frees for another worker if this one dies (mirrors the queue
            # monitor). Best-effort — a Redis blip shouldn't stop the sweep.
            try:
                rc = get_redis_client()
                if rc:
                    rc.set(_WATCHDOG_LOCK_KEY, '1', ex=_WATCHDOG_LOCK_TTL)
            except Exception:
                pass
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

    Called once per worker at app startup from app.__init__. DEPLOY-H4: under
    ``gunicorn --workers 4`` this fires in every worker, so 4 reapers used to
    run concurrently and emit duplicate events (the "4x in logs" symptom).
    A Redis ``SET NX EX`` lock — the same guard the queue monitor uses — lets
    exactly one worker own the watchdog; the loop refreshes the lock and it
    expires if that worker dies so another can take over.
    """
    from app import socketio
    from app.services.redis_service import get_redis_client

    try:
        rc = get_redis_client()
        if rc is not None:
            acquired = rc.set(_WATCHDOG_LOCK_KEY, '1', nx=True, ex=_WATCHDOG_LOCK_TTL)
            if not acquired:
                logger.info("[call_watchdog] already running in another worker — not starting")
                return
    except Exception as e:
        # If Redis is unreachable, fall through and start anyway — a possibly
        # duplicated watchdog is better than none (idempotent reaping).
        logger.warning(f"[call_watchdog] lock check failed ({e}) — starting unguarded")

    socketio.start_background_task(_run_loop, app)
