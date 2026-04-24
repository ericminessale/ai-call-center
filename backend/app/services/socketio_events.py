from flask_socketio import emit, join_room, leave_room
from flask import request
from app import socketio
from app.utils.jwt_utils import verify_token
from app.services.redis_service import add_to_set, remove_from_set
import logging

logger = logging.getLogger(__name__)


@socketio.on('connect')
def handle_connect(auth=None):
    """Handle client connection.

    Auto-authenticates using the token from connection auth if provided.
    This ensures the user joins their room even if the separate 'authenticate' event fails.
    """
    client_id = request.sid
    logger.info(f"Socket connected: {client_id} (auth_provided={bool(auth)})")

    # Try to auto-authenticate from connection auth
    token = None
    if auth and isinstance(auth, dict):
        token = auth.get('token')

    if token:
        user_id = verify_token(token)
        if user_id:
            # Join user's room automatically
            join_room(str(user_id))
            add_to_set(f"user:{user_id}:clients", request.sid)
            logger.info(f"Socket auto-auth: {client_id} -> user {user_id} (joined room '{user_id}')")
            emit('authenticated', {
                'message': 'Authentication successful',
                'user_id': user_id
            })
        else:
            logger.warning(f"Socket {client_id}: invalid token on connect")
    else:
        logger.debug(f"Socket {client_id}: no auth token on connect")

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


@socketio.on('set_agent_status')
def handle_set_agent_status(data):
    """Set agent availability status for call routing."""
    logger.info(f"set_agent_status received: {data}")

    token = data.get('token')
    status = data.get('status')  # 'available', 'busy', 'break', 'offline'

    if not token or not status:
        logger.warning("set_agent_status: missing token or status")
        emit('error', {'message': 'Missing token or status'})
        return

    if status not in ['available', 'busy', 'after-call', 'break', 'offline']:
        emit('error', {'message': 'Invalid status'})
        return

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return

    # Update Redis with agent status
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client

    redis_client = get_redis_client()
    if redis_client:
        queue_service = QueueService(redis_client)
        queue_service.set_agent_status(str(user_id), status)

        logger.info(f"Agent {user_id} set status to {status}")

        emit('agent_status_updated', {
            'status': status,
            'user_id': user_id
        })

        # Broadcast to all clients that agent status changed
        socketio.emit('agent_online_status', {
            'agent_id': user_id,
            'status': status
        })
    else:
        emit('error', {'message': 'Redis not available'})


@socketio.on('get_agent_status')
def handle_get_agent_status(data):
    """Get current agent status."""
    token = data.get('token')

    if not token:
        emit('error', {'message': 'Missing token'})
        return

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return

    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client

    redis_client = get_redis_client()
    if redis_client:
        queue_service = QueueService(redis_client)
        status_data = queue_service.get_agent_status(str(user_id))

        emit('agent_status', {
            'status': status_data.get('status', 'offline') if status_data else 'offline',
            'user_id': user_id
        })
    else:
        emit('agent_status', {'status': 'offline', 'user_id': user_id})


# Conference socket handlers
@socketio.on('join_conference')
def handle_join_conference(data):
    """Join a conference room to receive real-time updates."""
    conference_name = data.get('conference_name')
    token = data.get('token')

    if not conference_name or not token:
        emit('error', {'message': 'Missing conference_name or token'})
        return

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return

    # Join the conference room
    room_name = f'conference:{conference_name}'
    join_room(room_name)
    add_to_set(f"conference:{conference_name}:listeners", request.sid)

    emit('joined_conference', {
        'message': f'Joined conference room: {conference_name}',
        'conference_name': conference_name
    })

    logger.info(f"Client {request.sid} joined conference room: {room_name}")


@socketio.on('leave_conference')
def handle_leave_conference(data):
    """Leave a conference room."""
    conference_name = data.get('conference_name')

    if not conference_name:
        emit('error', {'message': 'Missing conference_name'})
        return

    room_name = f'conference:{conference_name}'
    leave_room(room_name)
    remove_from_set(f"conference:{conference_name}:listeners", request.sid)

    emit('left_conference', {
        'message': f'Left conference room: {conference_name}',
        'conference_name': conference_name
    })

    logger.info(f"Client {request.sid} left conference room: {room_name}")


@socketio.on('agent_answered')
def handle_agent_answered(data):
    """Handle agent answering a server-initiated call.

    When an agent answers a call from the backend, they need to be joined
    to the interaction conference via REST API. This is needed because
    Call Fabric subscribers don't support SWML url callbacks like phone calls do.
    """
    logger.info(f"agent_answered received: {data}")

    call_id = data.get('call_id')
    conference_name = data.get('conference_name')
    agent_id = data.get('agent_id')
    token = data.get('token')

    if not all([call_id, conference_name, token]):
        logger.warning("agent_answered: missing call_id, conference_name, or token")
        emit('error', {'message': 'Missing call_id, conference_name, or token'})
        return

    user_id = verify_token(token)
    if not user_id:
        emit('error', {'message': 'Invalid or expired token'})
        return

    try:
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()

        logger.info(f"agent_answered: joining call {call_id} to conference {conference_name}")

        # Join the agent's call to the conference
        result = sw_api.add_participant_to_conference(conference_name, call_id)

        logger.info(f"agent_answered: joined conference: {result}")

        emit('agent_joined_conference', {
            'conference_name': conference_name,
            'call_id': call_id,
            'success': True
        })

    except Exception as e:
        logger.exception(f"agent_answered: failed to join call {call_id} to conference {conference_name}: {e}")
        emit('error', {'message': f'Failed to join conference: {str(e)}'})