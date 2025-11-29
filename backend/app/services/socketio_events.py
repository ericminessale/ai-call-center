from flask_socketio import emit, join_room, leave_room
from flask import request
from app import socketio
from app.utils.jwt_utils import verify_token
from app.services.redis_service import add_to_set, remove_from_set
import logging

logger = logging.getLogger(__name__)


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    client_id = request.sid
    logger.info(f"Client connected: {client_id}")
    emit('connected', {'message': 'Connected to SignalWire Transcription Service'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    client_id = request.sid
    logger.info(f"Client disconnected: {client_id}")
    # Clean up any room memberships
    remove_from_set(f"active_clients", client_id)


@socketio.on('authenticate')
def handle_authenticate(data):
    """Authenticate WebSocket connection."""
    token = data.get('token')
    if not token:
        emit('error', {'message': 'No token provided'})
        return False

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return False

    # Join user's room (MUST be string to match emit room format)
    join_room(str(user_id))
    add_to_set(f"user:{user_id}:clients", request.sid)

    emit('authenticated', {
        'message': 'Authentication successful',
        'user_id': user_id
    })

    logger.info(f"Client authenticated: {request.sid} -> User: {user_id}, joined room '{str(user_id)}'")
    return True


@socketio.on('join_call')
def handle_join_call(data):
    """Join a call room to receive real-time updates."""
    call_sid = data.get('call_sid')
    token = data.get('token')

    if not call_sid or not token:
        emit('error', {'message': 'Missing call_sid or token'})
        return

    # Verify user has access to this call
    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return

    # Join the call room
    join_room(call_sid)
    add_to_set(f"call:{call_sid}:listeners", request.sid)

    emit('joined_call', {
        'message': f'Joined call room: {call_sid}',
        'call_sid': call_sid
    })

    logger.info(f"Client {request.sid} joined call room: {call_sid}")


@socketio.on('leave_call')
def handle_leave_call(data):
    """Leave a call room."""
    call_sid = data.get('call_sid')

    if not call_sid:
        emit('error', {'message': 'Missing call_sid'})
        return

    # Leave the call room
    leave_room(call_sid)
    remove_from_set(f"call:{call_sid}:listeners", request.sid)

    emit('left_call', {
        'message': f'Left call room: {call_sid}',
        'call_sid': call_sid
    })

    logger.info(f"Client {request.sid} left call room: {call_sid}")


@socketio.on('ping')
def handle_ping():
    """Handle ping to keep connection alive."""
    emit('pong', {'timestamp': request.sid})