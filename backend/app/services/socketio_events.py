from flask_socketio import emit, join_room, leave_room
from flask import request
from app import socketio
from app.models import Call, User
from app.utils.jwt_utils import verify_token
from app.services.redis_service import add_to_set, remove_from_set
from app.services.ws_rooms import WS_CLIENTS_PREFIX, workspace_room, ws_clients_key
import logging

logger = logging.getLogger(__name__)


def _join_workspace_room(user_id) -> None:
    """Join the authenticated socket to its workspace room (§8.1).

    Workspace-scoped emits (queue_update, call_update, call_ended,
    wallboard, config changes, …) all target ``ws:{workspace_id}`` — this
    server-side join is what makes them reach the user, on BOTH parallel
    frontend sockets, with no client-side join protocol. Platform users
    (workspace NULL — every clone-and-own user, plus the hosted operator)
    land in the default workspace's room, which in clone-and-own is the
    whole floor. Also records the sid in ``ws_clients:{id}`` so the
    wallboard knows which workspaces have watchers.
    """
    try:
        user = User.find_by_id(user_id)
        ws_id = user.workspace_id if user else None
        join_room(workspace_room(ws_id))
        add_to_set(ws_clients_key(ws_id), request.sid)
    except Exception as e:
        # A failed workspace join degrades realtime (no dashboard pushes)
        # but must not kill the connection handshake.
        logger.warning(f"workspace room join failed for user {user_id}: {e}")


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
            _join_workspace_room(user_id)
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
    """Handle client disconnection — clean up Redis sets + offline the agent
    if this was their last live socket.

    RT-04 fix (2026-06-02 audit): the previous handler only touched a
    generic ``active_clients`` set, leaving three classes of stale state:
      1. Per-user client tracking: ``user:<id>:clients`` accumulated
         orphan SIDs forever — every reconnect added a new entry, none
         were removed on disconnect.
      2. Per-call listener tracking: ``call:<sid>:listeners`` and
         ``conference:<name>:listeners`` were add-only.
      3. Agent status: if an agent's tab crashed, they stayed in
         ``agents:available`` indefinitely. Router would dispatch new
         calls to a dead socket — they'd 401/timeout and the caller
         would sit on hold.
    We now (a) scan our per-user client set for any room this socket
    joined, (b) discard the socket's SID from those sets, (c) if the
    user has no other live sockets, flip their queue-service status to
    ``offline`` so routing stops considering them. Best-effort — Redis
    is the source of truth and any failure here is logged not raised.
    """
    client_id = request.sid
    logger.info(f"Client disconnected: {client_id}")

    # Clean up the legacy global active_clients set (kept for back-compat).
    try:
        remove_from_set("active_clients", client_id)
    except Exception:
        pass

    # Identify which user (if any) this socket belonged to. We scan the
    # user:*:clients sets — could store reverse mapping for O(1) instead
    # if this becomes a hot path, but disconnects aren't frequent.
    try:
        from app.services.redis_service import get_redis_client
        rdb = get_redis_client()
        if not rdb:
            return
        owning_user_id = None
        # SCAN keyspace for user:*:clients sets that contain this sid.
        # Per-call/conference cleanup uses the same scan.
        for raw_key in rdb.scan_iter('user:*:clients'):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            if rdb.srem(key, client_id):
                # Got a hit — extract the user_id from the key shape
                # 'user:<id>:clients'.
                try:
                    owning_user_id = key.split(':', 2)[1]
                except (IndexError, ValueError):
                    pass
                # Don't break — a misconfigured client could have
                # SIDs in multiple user buckets; clean all.

        # Also drop this sid from any call/conference listener sets it
        # had joined. join_call/join_conference add the sid to these;
        # without explicit leave_* on a network blip the entries leak.
        for prefix in ('call:', 'conference:'):
            for raw_key in rdb.scan_iter(f'{prefix}*:listeners'):
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                rdb.srem(key, client_id)

        # And from the workspace client sets — the wallboard reads these to
        # decide which workspaces still have watchers.
        for raw_key in rdb.scan_iter(f'{WS_CLIENTS_PREFIX}*'):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            rdb.srem(key, client_id)

        # Offline the agent if this was their last live socket — i.e.
        # the per-user client set is now empty.
        if owning_user_id is not None:
            remaining = rdb.scard(f'user:{owning_user_id}:clients') or 0
            if remaining == 0:
                try:
                    from app.services.queue_service import QueueService
                    qs = QueueService(rdb)
                    # Only demote if they were tracking-live; preserves
                    # explicitly-set 'break' or 'offline' states.
                    state = qs.get_agent_status(str(owning_user_id))
                    if state and state.get('status') in ('available', 'busy', 'after-call'):
                        qs.set_agent_status(str(owning_user_id), 'offline')
                        logger.info(
                            f"Disconnect cleanup: agent {owning_user_id} "
                            f"had no remaining sockets → offline"
                        )
                except Exception as e:
                    logger.warning(
                        f"Disconnect cleanup: queue_service offline for "
                        f"user {owning_user_id} failed (non-fatal): {e}"
                    )
    except Exception as e:
        # Cleanup failure must not propagate — disconnect handlers run
        # in tight loops on connection churn and a Redis blip shouldn't
        # crash the worker.
        logger.warning(f"Disconnect cleanup failed for {client_id}: {e}")


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
    _join_workspace_room(user_id)

    emit('authenticated', {
        'message': 'Authentication successful',
        'user_id': user_id
    })

    logger.info(f"Client authenticated: {request.sid} -> User: {user_id}, joined room '{str(user_id)}'")
    return True


@socketio.on('join_call')
def handle_join_call(data):
    """Join a call room to receive real-time updates.

    ISO-3 (2026-07-07 pre-deploy): the call room carries the live
    transcript, AI context, KB facts and coaching for the call. This
    handler used to verify the token then join the room with NO check
    that the user was actually on the call — so on the shared demo any
    leased persona could join another visitor's room (given an
    ai_active call_sid, enumerable via list_calls) and passively receive
    their conversation. Now we resolve the call and require the requester
    to own it (initiated / assigned) OR hold a listen permission
    (supervisor/admin), mirroring join_tap's authorization.
    """
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

    user = User.find_by_id(user_id)
    if not user:
        emit('error', {'message': 'Invalid or expired token'})
        return

    # Resolve the call (accept DB id or SignalWire call_id) and authorize.
    call = None
    if str(call_sid).isdigit():
        call = Call.query.filter_by(id=int(call_sid)).first()
    if not call:
        call = Call.find_by_sid(str(call_sid))
    if not call:
        # Don't disclose whether the room exists vs. auth failed.
        emit('error', {'message': 'Call not available'})
        return

    # Tenancy predicate (§9: safety comes from scope, not flags): a
    # workspace-bound user may only enter rooms for calls in their own
    # workspace. Socket handlers run with no g.workspace_id, so the ORM
    # auto-filter doesn't apply here — enforce explicitly. Platform users
    # (workspace NULL) see across workspaces by design. Same message as
    # the not-found path so cross-tenant probes can't distinguish
    # "exists elsewhere" from "doesn't exist".
    if user.workspace_id is not None and call.workspace_id != user.workspace_id:
        emit('error', {'message': 'Call not available'})
        return

    role = user.role or ''
    is_owner = (call.user_id == user.id) or (call.assigned_agent_id == user.id)
    is_privileged = role in ('admin', 'supervisor')
    is_human_call = bool(call.conference_name and call.handler_type == 'human')
    listen_flag = 'can_listen_human_calls' if is_human_call else 'can_listen_ai_calls'
    # The old persona self-scope layer and shared-floor allowance are gone
    # with the shared floor itself (§10.4): a hosted visitor is the admin
    # of their own workspace, so within the workspace plain flag/role
    # semantics apply — identical to clone-and-own.
    flag_grants = user.has_permission(listen_flag)
    authorized = is_owner or is_privileged or flag_grants

    if not authorized:
        logger.warning(
            f"join_call: user {user_id} denied room for call {call.id} "
            f"(owner={is_owner}, role={role})"
        )
        emit('error', {'message': 'Not authorized for this call'})
        return

    # Join the call room. Producers key emits by SignalWire call_id, so
    # join under that (falling back to the supplied identifier).
    room = call.signalwire_call_sid or str(call_sid)
    join_room(room)
    add_to_set(f"call:{room}:listeners", request.sid)

    emit('joined_call', {
        'message': f'Joined call room: {room}',
        'call_sid': room
    })

    logger.info(f"Client {request.sid} joined call room: {room} (call DB id {call.id})")


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

        # Tell the agent's workspace that their status changed (was a global
        # broadcast — a visitor's presence is nobody else's wallboard data).
        status_user = User.find_by_id(user_id)
        socketio.emit('agent_online_status', {
            'agent_id': user_id,
            'status': status
        }, room=workspace_room(status_user.workspace_id if status_user else None))
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

    # Tenancy predicate (§8.1): this handler used to check only token
    # validity, so any authenticated socket could join any conference room
    # by name (names are derivable: interaction-<call_sid>). A workspace-
    # bound user may only enter rooms for conferences whose call is in
    # their own workspace; unresolvable conference/call fails CLOSED for
    # them. Platform users (workspace NULL — clone-and-own users and the
    # hosted operator) keep today's behavior. Same error message as any
    # other failure so probes can't distinguish.
    user = User.find_by_id(user_id)
    if not user:
        emit('error', {'message': 'Invalid or expired token'})
        return
    if user.workspace_id is not None:
        from app.models import Conference
        conference = Conference.get_active_by_name(conference_name)
        conf_call = Call.find_by_sid(conference.call_id) if conference else None
        if conf_call is None or conf_call.workspace_id != user.workspace_id:
            logger.warning(
                f"join_conference: user {user_id} denied room for "
                f"conference {conference_name} (cross-workspace or unresolvable)"
            )
            emit('error', {'message': 'Conference not available'})
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

    # ISO-7 (2026-07-07 pre-deploy): this bridges a client-supplied leg
    # (call_id) into a client-supplied conference. Without a check, a caller
    # could bridge an arbitrary leg into any conference (conference_name is
    # derivable). Verify the conference belongs to a call this user is the
    # assigned agent / owner of (or the user is supervisor/admin).
    user = User.find_by_id(user_id)
    if not user:
        emit('error', {'message': 'Invalid or expired token'})
        return
    from app.models import Conference
    conference = Conference.get_active_by_name(conference_name)
    conf_call = Call.find_by_sid(conference.call_id) if conference else None
    # Tenancy predicate (deviation 22): every hosted visitor is 'admin' of
    # their own workspace, so the role check below no longer bounds this
    # handler — without a workspace gate a visitor could bridge a leg into
    # another workspace's live conference. Workspace-bound users must
    # resolve the conference to a call in THEIR workspace; platform users
    # (workspace NULL) stay unscoped.
    if user.workspace_id is not None and (
        conf_call is None or conf_call.workspace_id != user.workspace_id
    ):
        logger.warning(
            f"agent_answered: user {user_id} denied cross-workspace bridge "
            f"into conference {conference_name}"
        )
        emit('error', {'message': 'Not authorized for this conference'})
        return
    role = user.role or ''
    authorized = role in ('admin', 'supervisor') or (
        conf_call is not None
        and (conf_call.assigned_agent_id == user.id or conf_call.user_id == user.id)
    )
    if not authorized:
        logger.warning(
            f"agent_answered: user {user_id} denied bridging leg {call_id} "
            f"into conference {conference_name}"
        )
        emit('error', {'message': 'Not authorized for this conference'})
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