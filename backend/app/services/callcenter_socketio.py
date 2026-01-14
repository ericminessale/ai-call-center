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

def is_demo_mode():
    """Check if system is in demo mode."""
    import os
    # Demo mode is enabled by default for development
    # Set DEMO_MODE=false in production
    return os.getenv('DEMO_MODE', 'true').lower() == 'true'


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

    # Emit to the general calls room (for supervisors and dashboards)
    socketio.emit('call_update', {'call': call_data})

    # If there's an assigned user, also emit to their personal room
    if call.user_id:
        socketio.emit('call_update', {'call': call_data}, room=str(call.user_id))

    # Emit to the call-specific room if there's a call SID
    if call.signalwire_call_sid:
        socketio.emit('call_update', {'call': call_data}, room=call.signalwire_call_sid)

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

    # Store in Redis for persistence
    redis_client = get_redis_client()
    if redis_client:
        redis_client.hset(f'agent:{user_id}', mapping={
            'status': status,
            'last_update': datetime.utcnow().isoformat()
        })
    else:
        logger.warning("Redis not available for agent status update")

    # Broadcast status change to supervisors
    socketio.emit('agent_status_update', {
        'agent_id': user_id,
        'status': status,
        'timestamp': datetime.utcnow().isoformat()
    }, room='supervisors')

    logger.info(f"Agent {user_id} status changed to: {status}")

    # If agent is available, check for queued calls
    # NOTE: In production, this auto-assigns real calls from the queue
    # For demo mode, we only auto-assign if there are demo calls in the queue
    if status == 'available':
        # Only auto-assign if not in demo mode or if explicitly requested
        if not is_demo_mode():
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
    """Handle call transfer."""
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    call_id = data.get('callId')
    destination = data.get('destination')
    transfer_type = data.get('type', 'cold')
    notes = data.get('notes', '')
    context = data.get('context', {})

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

    # Log transfer
    logger.info(f"Transfer initiated: Call {call_id} to {destination} ({transfer_type})")

    # Update call record (only for real calls, not demo)
    if not call_id.startswith('demo_'):
        try:
            # Try by database ID if numeric, otherwise by SignalWire SID
            call = None
            if str(call_id).isdigit():
                call = Call.query.filter_by(id=int(call_id)).first()
            if not call:
                call = Call.find_by_sid(call_id)
            if call:
                # Add transfer to history
                transfer_history = json.loads(call.transfer_history or '[]')
                transfer_history.append({
                    'from': user_id,
                    'to': destination,
                    'type': transfer_type,
                    'notes': notes,
                    'timestamp': datetime.utcnow().isoformat()
                })
                call.transfer_history = json.dumps(transfer_history)
                db.session.commit()
        except Exception as e:
            logger.error(f"Error updating transfer history: {e}")

    # Emit transfer event
    socketio.emit('call_transferred', {
        'call_id': call_id,
        'from_agent': user_id,
        'to': destination,
        'type': transfer_type,
        'context': context
    }, room=destination)

    emit('transfer_complete', {'call_id': call_id})


@socketio.on('hold_call')
def handle_hold_call(data):
    """Handle call hold/resume."""
    call_id = data.get('callId')
    hold = data.get('hold', True)

    # Broadcast hold status
    socketio.emit('call_hold_status', {
        'call_id': call_id,
        'on_hold': hold
    }, room=call_id)

    logger.info(f"Call {call_id} {'on hold' if hold else 'resumed'}")


@socketio.on('end_call')
def handle_end_call(data):
    """Handle call end."""
    token = data.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    call_id = data.get('callId')

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid token'})
        return

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
                call.status = 'ended'
                call.ended_at = datetime.utcnow()
                db.session.commit()
        except Exception as e:
            logger.error(f"Error ending call: {e}")

    # Notify all listeners
    socketio.emit('call_ended', {
        'call_id': call_id,
        'agent_id': user_id
    }, room=call_id)

    # Update agent status to after-call
    handle_agent_status_change({'status': 'after-call', 'token': token})


def check_and_assign_queued_call(agent_id: str) -> Optional[dict]:
    """Check queues and assign next call to available agent."""
    # Check each queue for waiting calls
    queues = ['sales', 'support', 'billing']

    for queue_id in queues:
        call_data = dequeue_call(queue_id, agent_id)
        if call_data:
            # Send call assignment
            socketio.emit('call_assigned', {
                'call': call_data['call'],
                'context': call_data['context']
            }, room=agent_id)
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

    for queue_id in ['sales', 'support', 'billing']:
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
            'slaCompliance': 85 if severity == 'normal' else 70 if severity == 'warning' else 50,
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
        while True:
            try:
                # Refresh the lock
                if redis_client:
                    redis_client.set('queue_monitor_lock', '1', ex=10)
                broadcast_queue_updates()
            except Exception as e:
                logger.error(f"Error broadcasting queue updates: {e}")
            time.sleep(5)  # Update every 5 seconds

    thread = Thread(target=monitor_queues, daemon=True)
    thread.start()
    _monitor_started = True
    logger.info("Queue monitor started")


# Don't start automatically on import - let the app context handle it