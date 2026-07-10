"""
Call Center WebSocket Events
Handles real-time updates for agents, queues, and calls
"""

from flask_socketio import emit, join_room, leave_room
from flask import request
from app import socketio, db
from app.utils.jwt_utils import verify_token
from app.models import User, Call
from app.services.redis_service import get_redis_client
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Agent status tracking
agent_statuses: Dict[str, dict] = {}

def _skip_auto_queue_assign() -> bool:
    """Whether to suppress auto-assigning queued calls when an agent goes
    available (they'd then claim manually via the request_next_call event).

    This used to be wired to ``DEMO_MODE`` under a function called
    ``is_demo_mode()``. That conflated two unrelated concepts:
      - ``DEMO_MODE`` (in app.utils.demo_config) = "this instance is the
        public hosted demo" — landing card, lease pool, etc.
      - This switch = "this instance is somebody's local sandbox; don't
        aggressively shove queued calls at them when they go available."

    For the hosted public demo we actually WANT auto-assign (otherwise a
    visitor who goes online sits there with nothing happening — bad demo).
    So the two are independent and the old shared name was wrong.

    Default ``false`` — production and the hosted demo both auto-assign.
    Operators running a local sandbox who want the old behavior set
    ``SKIP_AUTO_QUEUE_ASSIGN=true``.
    """
    import os
    return os.getenv('SKIP_AUTO_QUEUE_ASSIGN', '').strip().lower() == 'true'


def emit_call_update(call):
    """Emit a call update event to all relevant listeners.

    This notifies the frontend of call status changes so the UI updates in real-time.
    """
    if not call:
        return

    # Convert call to dict for emission
    call_data = call.to_dict() if hasattr(call, 'to_dict') else {
        'id': call.id,
        'status': call.status,
        'handler_type': call.handler_type,
        'from_number': call.from_number,
        'destination': call.destination,
        'signalwire_call_sid': call.signalwire_call_sid,
    }

    logger.info(f"Emitting call_update for call {call.id}, status: {call.status}")

    # Tenancy privacy (replaces the persona-owned check the shared floor
    # used): in hosted mode a call's updates carry caller numbers, AI
    # context and notes — they must never hit the global broadcast every
    # visitor's socket receives. Scope to the involved users' personal
    # rooms + the call room (which only workspace-authorized sockets can
    # join). Clone-and-own keeps the original global broadcast that
    # supervisor dashboards rely on; Phase 3's workspace rooms replace
    # this per-user targeting.
    from app.utils.demo_config import tenancy_mode_active
    private = tenancy_mode_active()

    # Emit to the general calls room (for supervisors and dashboards) — only
    # outside hosted mode.
    if not private:
        socketio.emit('call_update', {'call': call_data})

    # If there's an assigned user, also emit to their personal room
    if call.user_id:
        socketio.emit('call_update', {'call': call_data}, room=str(call.user_id))
    # Also target the assigned agent's room (post-takeover), so the human
    # who took it keeps getting updates.
    if private and call.assigned_agent_id and call.assigned_agent_id != call.user_id:
        socketio.emit('call_update', {'call': call_data}, room=str(call.assigned_agent_id))

    # Emit to the call-specific room if there's a call SID
    if call.signalwire_call_sid:
        socketio.emit('call_update', {'call': call_data}, room=call.signalwire_call_sid)

def emit_call_event(call_id, event_type, data, call_sid=None):
    """Emit a structured call event for the live event stream.

    Used by call_control.py, webhooks.py, calls.py to send real-time events
    that populate the Call Event Stream panel in the frontend.

    Args:
        call_id: Database call ID
        event_type: Category (state_change, hold, record, play, dtmf, monitor, conference, ai_tool_call, sentiment, transcription)
        data: Event-specific payload dict
        call_sid: Optional SignalWire call SID for room targeting
    """
    event = {
        'call_id': call_id,
        'event_type': event_type,
        'data': data,
        'timestamp': datetime.utcnow().isoformat(),
    }
    # Emit to call-specific room (authorized joiners only)
    if call_sid:
        socketio.emit('call_event', event, room=call_sid)

    # Tenancy privacy (replaces the persona-owned check): in hosted mode
    # never broadcast call events to every socket — scope to the involved
    # users' rooms (the call room above already reaches authorized
    # joiners). If the call can't be resolved, drop rather than leak.
    # Clone-and-own keeps the original global broadcast that supervisor
    # panels rely on; Phase 3's workspace rooms replace this.
    from app.utils.demo_config import tenancy_mode_active
    if tenancy_mode_active():
        try:
            owner_call = Call.query.get(int(call_id)) if str(call_id).isdigit() else None
        except Exception:
            owner_call = None
        if owner_call is not None:
            if owner_call.user_id:
                socketio.emit('call_event', event, room=str(owner_call.user_id))
            if owner_call.assigned_agent_id and owner_call.assigned_agent_id != owner_call.user_id:
                socketio.emit('call_event', event, room=str(owner_call.assigned_agent_id))
    else:
        # Broadcast globally for supervisor panels.
        socketio.emit('call_event', event)
    logger.debug(f"Call event emitted: {event_type} for call {call_id}")


@socketio.on('agent_status')
def handle_agent_status_change(data):
    """Handle agent status changes."""
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    status = data.get('status')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    # Update agent status in memory and Redis
    agent_statuses[user_id] = {
        'status': status,
        'timestamp': datetime.utcnow().isoformat(),
        'socket_id': request.sid
    }

    # Store in Redis using QueueService for consistent status tracking
    # This ensures agents:available set is properly managed
    redis_client = get_redis_client()
    if redis_client:
        from app.services.queue_service import QueueService
        queue_svc = QueueService(redis_client)
        queue_svc.set_agent_status(str(user_id), status)
    else:
        logger.warning("Redis not available for agent status update")

    # NOTE: a former 'agent_status_update' emit to room='supervisors' was
    # removed here (Phase 0 cleanup) — no handler ever joined that room, so
    # it reached nobody. Supervisor UIs consume the 'agent_online_status'
    # broadcast from socketio_events instead; when tenancy lands, THAT emit
    # moves to the workspace room.
    logger.info(f"Agent {user_id} status changed to: {status}")

    # When an agent goes available, optionally auto-assign the next
    # queued call. The opt-out (``SKIP_AUTO_QUEUE_ASSIGN=true``) exists
    # for local-sandbox runs where the developer doesn't want a stale
    # queued call thrown at them as soon as they flip online — they'll
    # claim manually via the request_next_call event instead.
    if status == 'available' and not _skip_auto_queue_assign():
        check_and_assign_queued_call(user_id)


@socketio.on('request_next_call')
def handle_request_next_call(data=None):
    """Agent manually requests next call from queue."""
    if data is None:
        data = {}
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    # Check queues for next call (works for both demo and real calls)
    assigned_call = check_and_assign_queued_call(user_id)

    if not assigned_call:
        emit('no_calls_waiting', {'message': 'No calls in queue'})


@socketio.on('take_call')
def handle_take_call(data):
    """Agent takes a specific call from queue."""
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    queue_id = data.get('queueId')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    # Get next call from specific queue
    call_data = dequeue_call(queue_id, user_id)

    if call_data:
        # Send call assignment to agent
        emit('call_assigned', {
            'call': call_data['call'],
            'context': call_data['context']
        }, room=request.sid)

        # Update agent status
        handle_agent_status_change({'status': 'busy', 'token': token})
    else:
        emit('no_calls_in_queue', {'queue_id': queue_id})


@socketio.on('transfer_call')
def handle_transfer_call(data):
    """Handle call transfer — NOT YET IMPLEMENTED (LIFE-02, 2026-06-02 audit).

    The previous implementation wrote to ``transfer_history`` and
    emitted ``call_transferred`` but never moved the participant on
    SignalWire's side. Stack-wide lie. Disabled until the real path is
    wired through ``conferences.move_participant``. Mirror of the
    /api/queues/transfer REST endpoint disablement.
    """
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return
    logger.warning(
        f"transfer_call: disabled stub fired by user {user_id} "
        f"(see LIFE-02 in REMEDIATION_2026-06-02.md)"
    )
    emit('error', {
        'message': 'Transfer not implemented',
        'detail': 'Use return-to-queue or request-backup until the real transfer path lands.',
    })


# hold_call socket handler removed (Phase 0 pre-tenancy cleanup, 2026-07-07):
# the Hold button was removed with the platform hold limitation (RE-AUDIT-01),
# no frontend emitter exists, and the handler only rebroadcast a cosmetic
# status into a room it mistargeted (DB id vs sid). Git history has it if
# participant-level hold ever lands platform-side.


@socketio.on('reject_call_assignment')
def handle_reject_call_assignment(data):
    """Agent declined the incoming call banner. Re-queue the call so another
    agent can pick it up; reset declining agent's status from busy → available.

    Without this handler the call would stay ``assigned`` indefinitely:
    `assigned_agent_id` still pointing at the declining agent, no other
    agent ever gets a shot, the caller holds forever, and the Redis queue
    counter is permanently inflated. This is the bug that produces the
    "you are number 2 in queue" when only one caller is actually waiting.
    """
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    call_id = data.get('call_id')
    conference_name = data.get('conference_name')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    logger.info(
        f"Agent {user_id} declined call assignment "
        f"(call_id={call_id}, conf={conference_name})"
    )

    if not call_id:
        emit('error', {'message': 'call_id required'})
        return

    call = None
    if str(call_id).isdigit():
        call = Call.query.filter_by(id=int(call_id)).first()
    if not call:
        call = Call.find_by_sid(call_id)
    if not call:
        logger.warning(f"Reject: call {call_id} not found")
        return

    queue_id = call.queue_id
    declining_agent = str(user_id)

    try:
        from app.services.queue_service import QueueService
        from app.services.redis_service import get_redis_client
        qs = QueueService(get_redis_client())

        # Record the decline BEFORE we flip the agent to available below.
        # Otherwise set_agent_status('available') re-triggers push-dispatch,
        # which sees the same call at the queue head, picks the same agent
        # (single-agent scenarios), and we ring them again — the infinite
        # banner loop the user reported.
        qs.mark_decline(declining_agent, call.signalwire_call_sid)

        # Reset assignment so another agent can take it.
        call.assigned_agent_id = None
        call.status = 'waiting'
        db.session.commit()

        # Re-add to the queue zset (was removed when this agent was assigned).
        if queue_id:
            try:
                context = json.loads(call.ai_context) if call.ai_context else {}
            except (ValueError, TypeError):
                context = {}
            qs.enqueue_call(
                call_id=call.signalwire_call_sid,
                queue_id=queue_id,
                priority=context.get('priority', 5),
                context=context,
                caller_info={'number': call.from_number, 'name': None},
            )

        # Free the declining agent so they can take the next call (or this one
        # again, if no other agents — that's a separate "exclude declined-by"
        # feature). Only clear if Redis still thinks they're busy on THIS call.
        agent_state = qs.get_agent_status(declining_agent)
        if agent_state and agent_state.get('current_call_id') == call.signalwire_call_sid:
            qs.set_agent_status(declining_agent, 'available')

        # Notify dashboard listeners — call is back in the queue.
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': queue_id,
            'action': 'added',
        })

        logger.info(
            f"Re-queued call {call.id} into '{queue_id}' after agent "
            f"{declining_agent} declined"
        )
    except Exception as e:
        logger.error(f"Error processing reject_call_assignment: {e}")
        db.session.rollback()


@socketio.on('join_tap')
def handle_join_tap(data):
    """Authorize the requester for tap audio on a specific call.

    RT-01 (2026-06-02 audit follow-up). The tap audio stream
    (``tap_audio``/``tap_status``/``tap_metadata`` emits in
    :mod:`app.services.tap_relay`) is room-scoped to ``tap:{call_id}``.
    Sockets only see the stream after this handler validates them against
    the same permission check ``call_control.start_monitor`` uses for the
    REST monitor surface — so the "Listen" button maps to one auth model
    regardless of whether it's tap audio or conference silent-join.

    Request body: ``{token: str, call_id: str|int}``.

    Permission decision mirrors ``call_control.py:start_monitor``:
      - human-handled conference call → requires ``can_listen_human_calls``
      - everything else (AI sessions, tap-only monitoring) → requires
        ``can_listen_ai_calls``

    Failure modes — emit a ``tap_error`` event with a reason, do NOT
    join the room. (Errors are scoped to the SOCKET, not broadcast, so
    we don't leak that the call exists.)
    """
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    raw_call_id = data.get('call_id')

    user_id = verify_token(token)
    if not user_id:
        emit('tap_error', {'message': 'Invalid token'})
        return
    if not raw_call_id:
        emit('tap_error', {'message': 'call_id required'})
        return

    # Resolve to a Call row so we can pick the right permission flag.
    from app.models.call import Call
    from app.models.user import User
    call = None
    if str(raw_call_id).isdigit():
        call = Call.query.filter_by(id=int(raw_call_id)).first()
    if not call:
        call = Call.find_by_sid(str(raw_call_id))
    if not call:
        # Don't tell the caller whether the call exists or just failed
        # auth — both look the same to them.
        emit('tap_error', {'message': 'Tap not available'})
        return

    user = User.query.filter_by(id=user_id).first()
    if not user:
        emit('tap_error', {'message': 'Invalid token'})
        return

    # Tenancy predicate (§9): tap audio never crosses workspaces. Socket
    # handlers run unscoped, so enforce explicitly; platform users
    # (workspace NULL) monitor across by design. Same message as the
    # not-found path so probes can't distinguish.
    if user.workspace_id is not None and call.workspace_id != user.workspace_id:
        emit('tap_error', {'message': 'Tap not available'})
        return

    is_human_call = bool(call.conference_name and call.handler_type == 'human')
    required_flag = 'can_listen_human_calls' if is_human_call else 'can_listen_ai_calls'
    # Owner bypass: you may always listen to your OWN call (the call you
    # initiated or are assigned to). Otherwise fall back to the
    # listen-permission check (supervisors/admins monitoring others'
    # calls) — plain flag semantics inside the workspace, same as
    # clone-and-own; the old persona self-scope layer is gone with the
    # shared floor.
    is_owner = (call.user_id == user_id) or (call.assigned_agent_id == user_id)
    flag_grants = user.has_permission(required_flag)
    if not is_owner and not flag_grants:
        logger.warning(
            f"join_tap: user {user_id} lacks {required_flag} for call "
            f"{call.id} (human={is_human_call})"
        )
        emit('tap_error', {
            'message': 'Missing required permissions',
            'required_permissions': [required_flag],
        })
        return

    # The tap stream emits use the SignalWire call_id (string), not the
    # DB id. Join the room under that key so the producer's emits land
    # on this socket.
    room = f'tap:{call.signalwire_call_sid}'
    join_room(room)
    logger.info(f"join_tap: user {user_id} joined {room} (call DB id {call.id})")
    emit('tap_joined', {
        'call_id': call.signalwire_call_sid,
        'db_call_id': call.id,
    })


@socketio.on('leave_tap')
def handle_leave_tap(data):
    """Stop receiving tap audio for a call. Idempotent — leaving a room
    you're not in is a no-op. No auth needed; the worst a caller can do
    is leave a room they weren't authorized to be in anyway."""
    raw_call_id = data.get('call_id')
    if not raw_call_id:
        return
    # Accept either the DB id or the SignalWire sid; the producer keys
    # by sid, so resolve.
    from app.models.call import Call
    call = None
    if str(raw_call_id).isdigit():
        call = Call.query.filter_by(id=int(raw_call_id)).first()
    if not call:
        call = Call.find_by_sid(str(raw_call_id))
    sid = call.signalwire_call_sid if call else str(raw_call_id)
    leave_room(f'tap:{sid}')


@socketio.on('end_call')
def handle_end_call(data):
    """Handle call end."""
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    call_id = data.get('callId')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    call = None
    # Update call status (only for real calls, not demo)
    if not call_id.startswith('demo_'):
        try:
            # Try by database ID if numeric, otherwise by SignalWire SID
            call = None
            if str(call_id).isdigit():
                call = Call.query.filter_by(id=int(call_id)).first()
            if not call:
                call = Call.find_by_sid(call_id)
            if call:
                # ISO-8 (2026-07-07 pre-deploy): ownership gate — this handler
                # ended ANY call by id/sid with no check, so a persona could
                # hang up another visitor's live call. Only the owner/assigned
                # agent or a supervisor/admin may end it.
                user = User.find_by_id(user_id)
                role = (user.role if user else '') or ''
                is_owner = user and (
                    call.user_id == user.id or call.assigned_agent_id == user.id
                )
                if not (is_owner or role in ('admin', 'supervisor')):
                    logger.warning(
                        f"end_call socket: user {user_id} denied ending call {call.id}"
                    )
                    emit('error', {'message': 'Not authorized for this call'})
                    return
                call.status = 'ended'
                call.ended_at = datetime.utcnow()
                db.session.commit()
        except Exception as e:
            logger.error(f"Error ending call: {e}")

    # Notify listeners in the call room. join_call keys rooms by the
    # SignalWire sid — resolve it when the client passed a DB id, else
    # the emit targets a room with no members.
    socketio.emit('call_ended', {
        'call_id': call_id,
        'agent_id': user_id
    }, room=((call.signalwire_call_sid or call_id) if call else call_id))

    # Update agent status to after-call
    handle_agent_status_change({'status': 'after-call', 'token': token})


def check_and_assign_queued_call(agent_id: str) -> Optional[dict]:
    """Check queues and assign next call to available agent."""
    # Check each queue for waiting calls
    from app.models.queue import Queue
    queues = Queue.get_active_slugs()

    for queue_id in queues:
        call_data = dequeue_call(queue_id, agent_id)
        if call_data:
            # Send call assignment. Rooms are joined as str(user_id) and
            # agent_id arrives as the int from verify_token — an int room
            # here has no members.
            socketio.emit('call_assigned', {
                'call': call_data['call'],
                'context': call_data['context']
            }, room=str(agent_id))
            return call_data

    return None


def dequeue_call(queue_id: str, agent_id: str) -> Optional[dict]:
    """Get next call from queue and assign to agent."""
    redis_client = get_redis_client()
    if not redis_client:
        logger.error("Redis not available for dequeuing call")
        return None

    queue_key = f"queue:{queue_id}"

    # Get highest priority call from Redis sorted set
    calls = redis_client.zrange(queue_key, 0, 0)
    if not calls:
        return None

    call_data = json.loads(calls[0])
    redis_client.zrem(queue_key, calls[0])

    # Create mock call object for now
    # In production, this would come from the database
    call_obj = {
        'id': call_data.get('call_id', f'call_{datetime.utcnow().timestamp()}'),
        'customerName': call_data.get('customer_name', 'Unknown Caller'),
        'phoneNumber': call_data.get('phone_number', '+1234567890'),
        'startTime': datetime.utcnow().isoformat(),
        'status': 'active',
        'queueId': queue_id,
        'priority': call_data.get('priority', 'medium')
    }

    return {
        'call': call_obj,
        'context': call_data.get('context', {})
    }


def broadcast_queue_updates():
    """Broadcast queue statistics to all connected agents."""
    redis_client = get_redis_client()
    if not redis_client:
        logger.error("Redis not available for queue updates")
        return

    queues_data = []

    from app.models.queue import Queue
    for queue_id in Queue.get_active_slugs():
        queue_key = f"queue:{queue_id}"
        queue_depth = redis_client.zcard(queue_key)

        # Calculate wait times
        calls = redis_client.zrange(queue_key, 0, -1)
        wait_times = []
        now = datetime.utcnow()

        for call_json in calls:
            call_data = json.loads(call_json)
            enqueued = datetime.fromisoformat(call_data['enqueued_at'])
            wait_times.append((now - enqueued).total_seconds())

        avg_wait = sum(wait_times) / len(wait_times) if wait_times else 0
        longest_wait = max(wait_times) if wait_times else 0

        # Determine severity
        severity = 'critical' if queue_depth > 10 else 'warning' if queue_depth > 5 else 'normal'

        queues_data.append({
            'id': queue_id,
            'name': queue_id.capitalize(),
            'waiting': queue_depth,
            'avgWait': int(avg_wait),
            'longest': int(longest_wait),
            'severity': severity,
            'trend': 'stable',  # Calculate based on history
            'waitingCalls': []  # Add actual call previews if needed
        })

    # Broadcast to all connected agents
    socketio.emit('queue_update', queues_data)


# Schedule periodic queue updates
_monitor_started = False

def start_queue_monitor():
    """Start background task to broadcast queue updates."""
    global _monitor_started

    # Prevent multiple monitors
    if _monitor_started:
        return

    from threading import Thread
    import time

    # Try to acquire a lock in Redis
    redis_client = get_redis_client()
    if redis_client:
        try:
            # Set a key with NX (only if not exists) and EX (expire after 10 seconds)
            # This acts as a distributed lock
            lock_acquired = redis_client.set('queue_monitor_lock', '1', nx=True, ex=10)
            if not lock_acquired:
                logger.info("Queue monitor already running in another worker")
                return
        except Exception as e:
            logger.warning(f"Could not acquire queue monitor lock: {e}")

    def monitor_queues():
        # Import Flask app for context in background thread
        from app import create_app_context
        while True:
            try:
                # Refresh the lock
                if redis_client:
                    redis_client.set('queue_monitor_lock', '1', ex=10)
                # broadcast_queue_updates uses SQLAlchemy models (Queue),
                # so it needs the Flask app context in this background thread
                with create_app_context():
                    broadcast_queue_updates()
            except Exception as e:
                logger.error(f"Error broadcasting queue updates: {e}")
            time.sleep(5)  # Update every 5 seconds

    thread = Thread(target=monitor_queues, daemon=True)
    thread.start()
    _monitor_started = True
    logger.info("Queue monitor started")


# Don't start automatically on import - let the app context handle it