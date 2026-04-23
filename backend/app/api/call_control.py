"""
Call Control API Blueprint
Real-time call manipulation endpoints: hold, record, play, DTMF, monitor, backup, escalate.
"""

from flask import Blueprint, request, jsonify
from app import db, socketio
from app.models.call import Call
from app.models.call_leg import CallLeg
from app.models import User
from app.utils.decorators import require_auth, require_role
from app.services.signalwire_api import get_signalwire_api
from app.services.redis_service import get_redis_client
from datetime import datetime
import logging
import json
import os

logger = logging.getLogger(__name__)

call_control_bp = Blueprint('call_control', __name__)


# ==================== Helpers ====================

def find_call(call_id):
    """Find a call by database ID or SignalWire call SID."""
    # Try by database ID first
    try:
        call = Call.query.get(int(call_id))
        if call:
            return call
    except (ValueError, TypeError):
        pass
    # Try by SignalWire call SID
    return Call.find_by_sid(str(call_id))


def emit_call_event(call_id, event_type, data, call_sid=None):
    """Proxy to the central emit_call_event in callcenter_socketio."""
    from app.services.callcenter_socketio import emit_call_event as _emit_event
    _emit_event(call_id, event_type, data, call_sid)


# ==================== Call Control Endpoints ====================

@call_control_bp.route('/<call_id>/hold', methods=['POST'])
@require_auth
def hold_call(call_id):
    """Place an active call on hold."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        result = sw_api.hold_call(call.signalwire_call_sid)

        call.status = 'on_hold'
        db.session.commit()

        # Emit events
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)
        emit_call_event(call.id, 'hold', {
            'action': 'hold',
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'status': 'on_hold', 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to hold call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/unhold', methods=['POST'])
@require_auth
def unhold_call(call_id):
    """Resume a held call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        result = sw_api.unhold_call(call.signalwire_call_sid)

        call.status = 'active'
        db.session.commit()

        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)
        emit_call_event(call.id, 'hold', {
            'action': 'unhold',
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'status': 'active', 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to unhold call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/play', methods=['POST'])
@require_auth
def play_into_call(call_id):
    """Play audio or TTS into an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    data = request.get_json() or {}
    play_type = data.get('type', 'tts')

    try:
        sw_api = get_signalwire_api()

        if play_type == 'audio':
            url = data.get('url')
            if not url:
                return jsonify({'error': 'url is required for audio playback'}), 400
            result = sw_api.play_audio(call.signalwire_call_sid, url)
            event_data = {'action': 'play_audio', 'url': url}
        else:
            text = data.get('text')
            if not text:
                return jsonify({'error': 'text is required for TTS'}), 400
            voice = data.get('voice', 'en-US-Neural2-F')
            result = sw_api.play_tts(call.signalwire_call_sid, text, voice)
            event_data = {'action': 'play_tts', 'text': text, 'voice': voice}

        event_data['agent'] = request.current_user.email
        emit_call_event(call.id, 'play', event_data, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'type': play_type, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to play into call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/record/start', methods=['POST'])
@require_auth
def start_recording(call_id):
    """Start recording an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        result = sw_api.start_recording(call.signalwire_call_sid)

        # Store control_id in Redis for stopping later
        control_id = result.get('control_id')
        if control_id:
            redis_client = get_redis_client()
            redis_client.set(f'recording:{call.id}', control_id, ex=7200)  # 2 hour TTL

        emit_call_event(call.id, 'record', {
            'action': 'start',
            'agent': request.current_user.email,
            'control_id': control_id
        }, call.signalwire_call_sid)

        # Notify frontend of recording state
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        return jsonify({
            'success': True,
            'call_id': call.id,
            'recording': True,
            'control_id': control_id,
            'result': result
        }), 200
    except Exception as e:
        logger.error(f"Failed to start recording for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/record/stop', methods=['POST'])
@require_auth
def stop_recording(call_id):
    """Stop recording an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        # Get control_id from Redis
        redis_client = get_redis_client()
        control_id = redis_client.get(f'recording:{call.id}')

        sw_api = get_signalwire_api()
        result = sw_api.stop_recording(call.signalwire_call_sid, control_id)

        # Clean up Redis
        redis_client.delete(f'recording:{call.id}')

        emit_call_event(call.id, 'record', {
            'action': 'stop',
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        return jsonify({'success': True, 'call_id': call.id, 'recording': False, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to stop recording for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/dtmf', methods=['POST'])
@require_auth
def send_dtmf(call_id):
    """Send DTMF tones into an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    data = request.get_json() or {}
    digits = data.get('digits', '')

    # Validate digits
    import re
    if not re.match(r'^[0-9*#]+$', digits):
        return jsonify({'error': 'Invalid DTMF digits. Only 0-9, *, # are allowed.'}), 400

    try:
        sw_api = get_signalwire_api()
        result = sw_api.send_dtmf(call.signalwire_call_sid, digits)

        emit_call_event(call.id, 'dtmf', {
            'digits': digits,
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'digits': digits, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to send DTMF to call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Monitoring Endpoints ====================

@call_control_bp.route('/<call_id>/monitor/start', methods=['POST'])
@require_auth
# TODO: re-add @require_role('supervisor', 'admin') in auth overhaul
def start_monitor(call_id):
    """Start monitoring an active call (audio tap or silent conference join).

    For AI calls (non-conference): uses SignalWire tap to stream audio via WebSocket.
    For human calls (conference-based): prepares a silent conference join.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user
    redis_client = get_redis_client()

    try:
        if call.conference_name and call.handler_type == 'human':
            # Conference-based call: prepare silent join
            import uuid
            token = str(uuid.uuid4())
            redis_data = {
                'agent_id': str(user.id),
                'conf': call.conference_name,
                'call_id': str(call.id),
                'type': 'monitor',
                'muted': 'true',
                'beep': 'false',
            }
            redis_client.set(f'conference_join:{token}', json.dumps(redis_data), ex=300)

            base_url = os.environ.get('EXTERNAL_URL', 'http://localhost:5000')
            dial_address = f"{base_url}/api/conferences/agent-conference?token={token}"

            emit_call_event(call.id, 'monitor', {
                'action': 'start',
                'monitor_type': 'conference_silent_join',
                'agent': user.email
            }, call.signalwire_call_sid)

            return jsonify({
                'success': True,
                'monitor_type': 'conference',
                'dial_address': dial_address,
                'token': token,
                'conference_name': call.conference_name,
            }), 200
        else:
            # AI call or non-conference: use tap
            base_url = os.environ.get('EXTERNAL_URL', 'http://localhost:5000')
            ws_url = base_url.replace('http://', 'ws://').replace('https://', 'wss://')
            tap_uri = f"{ws_url}/ws/tap-stream/{call.id}"

            sw_api = get_signalwire_api()
            result = sw_api.tap_call(call.signalwire_call_sid, tap_uri, direction='both')

            # Store tap control_id
            control_id = result.get('control_id')
            if control_id:
                redis_client.set(f'tap:{call.id}:{user.id}', control_id, ex=7200)

            emit_call_event(call.id, 'monitor', {
                'action': 'start',
                'monitor_type': 'tap',
                'agent': user.email,
                'control_id': control_id
            }, call.signalwire_call_sid)

            return jsonify({
                'success': True,
                'monitor_type': 'tap',
                'control_id': control_id,
                'result': result,
            }), 200

    except Exception as e:
        logger.error(f"Failed to start monitoring call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/monitor/stop', methods=['POST'])
@require_auth
# TODO: re-add @require_role('supervisor', 'admin') in auth overhaul
def stop_monitor(call_id):
    """Stop monitoring an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user
    redis_client = get_redis_client()

    try:
        # Check for active tap
        control_id = redis_client.get(f'tap:{call.id}:{user.id}')
        if control_id:
            sw_api = get_signalwire_api()
            result = sw_api.stop_tap(call.signalwire_call_sid, control_id)
            redis_client.delete(f'tap:{call.id}:{user.id}')

            emit_call_event(call.id, 'monitor', {
                'action': 'stop',
                'monitor_type': 'tap',
                'agent': user.email
            }, call.signalwire_call_sid)

            return jsonify({'success': True, 'monitor_type': 'tap', 'result': result}), 200

        # For conference monitor, the agent just hangs up their Call Fabric connection
        emit_call_event(call.id, 'monitor', {
            'action': 'stop',
            'agent': user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'monitor_type': 'conference', 'message': 'Disconnect your Call Fabric client to stop monitoring'}), 200

    except Exception as e:
        logger.error(f"Failed to stop monitoring call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Multi-Agent Conferencing Endpoints ====================

@call_control_bp.route('/<call_id>/request-backup', methods=['POST'])
@require_auth
def request_backup(call_id):
    """Request a backup agent to join the current call's conference.

    Finds an available agent from the queue (excluding the requesting agent),
    and sends them a call assignment notification.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    if not call.conference_name:
        return jsonify({'error': 'Call must be in a conference to request backup'}), 400

    data = request.get_json() or {}
    queue_slug = data.get('queue_id') or call.queue_id or 'support'

    try:
        redis_client = get_redis_client()
        from app.services.queue_service import QueueService
        queue_service = QueueService(redis_client)

        # Get available agents excluding the requesting agent
        available = queue_service.get_available_agents(queue_slug)
        available = [a for a in available if str(a) != str(request.current_user.id)]

        if not available:
            return jsonify({'error': 'No agents available for backup'}), 404

        # Get queue for routing strategy
        from app.models.queue import Queue
        queue = Queue.query.filter_by(slug=queue_slug).first()
        strategy = queue.routing_strategy if queue else 'round_robin'
        skill_levels = {}
        if queue:
            from app.models.queue import QueueAgentAssignment
            assignments = QueueAgentAssignment.query.filter_by(queue_id=queue.id).all()
            skill_levels = {str(a.user_id): a.skill_level for a in assignments}

        selected_agent_id = queue_service.select_agent(queue_slug, strategy, available, skill_levels)

        if not selected_agent_id:
            return jsonify({'error': 'No suitable agent found'}), 404

        selected_user = User.query.get(int(selected_agent_id))
        if not selected_user:
            return jsonify({'error': 'Selected agent not found'}), 404

        # Create a backup leg record
        max_leg = db.session.query(db.func.max(CallLeg.leg_number)).filter_by(call_id=call.id).scalar() or 0
        backup_leg = CallLeg(
            call_id=call.id,
            leg_type='backup',
            leg_number=max_leg + 1,
            user_id=selected_user.id,
            status='connecting',
            conference_name=call.conference_name,
        )
        db.session.add(backup_leg)
        db.session.commit()

        # Emit assignment to the selected agent
        assignment_data = {
            'type': 'backup',
            'call': call.to_dict(include_contact=True),
            'requesting_agent': {
                'id': request.current_user.id,
                'name': request.current_user.name or request.current_user.email,
                'email': request.current_user.email,
            },
            'conference_name': call.conference_name,
            'leg_id': backup_leg.id,
            'call_db_id': call.id,
        }
        socketio.emit('call_assignment', assignment_data, room=str(selected_agent_id))

        emit_call_event(call.id, 'conference', {
            'action': 'backup_requested',
            'requesting_agent': request.current_user.email,
            'selected_agent': selected_user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'selected_agent_id': selected_user.id,
            'selected_agent_name': selected_user.name or selected_user.email,
            'leg_id': backup_leg.id,
        }), 200

    except Exception as e:
        logger.error(f"Failed to request backup for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/escalate', methods=['POST'])
@require_auth
def escalate_to_supervisor(call_id):
    """Escalate an active call to a supervisor.

    Finds an available supervisor/admin and sends them a call assignment notification.
    Supports whisper mode where the supervisor can only speak to the agent (not the customer).
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    if not call.conference_name:
        return jsonify({'error': 'Call must be in a conference to escalate'}), 400

    data = request.get_json() or {}
    whisper_mode = data.get('whisper', False)

    try:
        # Find available supervisors and admins
        candidates = User.query.filter(
            User.role.in_(['supervisor', 'admin']),
            User.is_active == True,
            User.id != request.current_user.id
        ).all()

        if not candidates:
            return jsonify({'error': 'No supervisors found in the system'}), 404

        # Check availability via Redis
        redis_client = get_redis_client()
        from app.services.queue_service import QueueService
        queue_service = QueueService(redis_client)
        available_ids = queue_service.get_available_agents()

        available_supervisors = [s for s in candidates if str(s.id) in available_ids]

        if not available_supervisors:
            # Fall back to all supervisors if none are "available" (they might not use queue system)
            available_supervisors = candidates

        # Pick first available
        supervisor = available_supervisors[0]

        # Create escalation leg
        max_leg = db.session.query(db.func.max(CallLeg.leg_number)).filter_by(call_id=call.id).scalar() or 0
        esc_leg = CallLeg(
            call_id=call.id,
            leg_type='supervisor',
            leg_number=max_leg + 1,
            user_id=supervisor.id,
            status='connecting',
            conference_name=call.conference_name,
        )
        db.session.add(esc_leg)
        db.session.commit()

        # Find the requesting agent's call SID for coach mode
        agent_call_sid = None
        if whisper_mode:
            agent_legs = CallLeg.query.filter_by(
                call_id=call.id,
                user_id=request.current_user.id,
                status='active'
            ).first()
            if agent_legs and agent_legs.signalwire_sid:
                agent_call_sid = agent_legs.signalwire_sid

        # Emit assignment to supervisor
        assignment_data = {
            'type': 'escalation',
            'call': call.to_dict(include_contact=True),
            'requesting_agent': {
                'id': request.current_user.id,
                'name': request.current_user.name or request.current_user.email,
                'email': request.current_user.email,
            },
            'conference_name': call.conference_name,
            'leg_id': esc_leg.id,
            'call_db_id': call.id,
            'whisper_mode': whisper_mode,
            'agent_call_sid': agent_call_sid,
        }
        socketio.emit('call_assignment', assignment_data, room=str(supervisor.id))

        emit_call_event(call.id, 'conference', {
            'action': 'escalation_requested',
            'requesting_agent': request.current_user.email,
            'supervisor': supervisor.email,
            'whisper_mode': whisper_mode,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'supervisor_id': supervisor.id,
            'supervisor_name': supervisor.name or supervisor.email,
            'leg_id': esc_leg.id,
            'whisper_mode': whisper_mode,
        }), 200

    except Exception as e:
        logger.error(f"Failed to escalate call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500
