"""
Call Control API Blueprint
Real-time call manipulation endpoints: hold, record, play, DTMF, monitor, backup, escalate.
"""

from flask import Blueprint, request, jsonify
from app import db, socketio
from app.models.call import Call
from app.models.call_leg import CallLeg
from app.models import User
from app.utils.decorators import require_auth, require_permission, require_role
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

def _find_agent_participant(call, user_id):
    """Locate the current agent's ConferenceParticipant in the call's conference.

    Returns the ConferenceParticipant record whose call_sid we can mute/deaf,
    or None if the call isn't in a conference or the agent isn't a member yet.
    """
    if not call or not call.conference_name:
        return None
    from app.models.conference import Conference
    from app.models.conference_participant import ConferenceParticipant
    conf = Conference.get_active_by_name(call.conference_name)
    if not conf:
        return None
    return (
        db.session.query(ConferenceParticipant)
        .filter_by(
            conference_id=conf.id,
            participant_type='agent',
            participant_id=str(user_id),
            status='active',
        )
        .first()
    )


@call_control_bp.route('/<call_id>/hold', methods=['POST'])
@require_auth
def hold_call(call_id):
    """Hold the call — cut the agent's audio both ways.

    Conference-aware: if the call is bridged through a conference (the common
    human-agent path), mute + deafen the AGENT's conference member. That
    cleanly blocks audio in both directions, caller stays connected, and the
    conference continues playing any configured hold media to remaining members.

    Falls back to legacy `calling.hold` on the caller leg for non-conference
    calls, with a log line so the operator knows what path was taken.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        user = request.current_user

        agent_participant = _find_agent_participant(call, user.id)
        if agent_participant and agent_participant.call_sid:
            # Announce the hold to the caller BEFORE cutting audio both ways,
            # so they don't hit a wall of silence. Failure is logged-not-raised
            # — the hold itself takes priority over the announcement.
            # TODO: localize the message based on call.caller_language when set.
            try:
                sw_api.play_tts(
                    call.signalwire_call_sid,
                    "Please hold. I'll be right back with you.",
                )
            except Exception as tts_err:
                logger.warning(
                    f"hold_call {call_id}: on-hold TTS announcement failed "
                    f"(continuing with mute): {tts_err}"
                )

            # Conference path: mute + deaf the agent's own member so nothing
            # crosses in either direction. Caller stays with the conference.
            sw_api.mute_participant(
                conference_name=call.conference_name,
                call_id=agent_participant.call_sid,
                muted=True,
                deaf=True,
            )
            agent_participant.is_muted = True
            agent_participant.is_deaf = True
            agent_participant.status = 'muted'
            path = 'conference-member'
            result = {'participant_call_sid': agent_participant.call_sid}
        else:
            # Non-conference fallback — pauses media on the caller leg.
            # Imperfect, but preserves the old behavior for call shapes we
            # haven't modeled yet. Logged so operators can tell.
            logger.warning(
                f"hold_call {call_id}: no conference participant for user {user.id} — "
                f"falling back to calling.hold on caller leg"
            )
            result = sw_api.hold_call(call.signalwire_call_sid)
            path = 'legacy-caller-leg'

        call.status = 'on_hold'
        db.session.commit()

        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)
        emit_call_event(call.id, 'hold', {
            'action': 'hold',
            'path': path,
            'agent': user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True, 'call_id': call.id, 'status': 'on_hold',
            'path': path, 'result': result,
        }), 200
    except Exception as e:
        logger.error(f"Failed to hold call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/unhold', methods=['POST'])
@require_auth
def unhold_call(call_id):
    """Resume a held call. Mirrors hold_call's two paths."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        user = request.current_user

        agent_participant = _find_agent_participant(call, user.id)
        if agent_participant and agent_participant.call_sid:
            sw_api.mute_participant(
                conference_name=call.conference_name,
                call_id=agent_participant.call_sid,
                muted=False,
                deaf=False,
            )
            agent_participant.is_muted = False
            agent_participant.is_deaf = False
            agent_participant.status = 'active'
            path = 'conference-member'
            result = {'participant_call_sid': agent_participant.call_sid}
        else:
            result = sw_api.unhold_call(call.signalwire_call_sid)
            path = 'legacy-caller-leg'

        call.status = 'active'
        db.session.commit()

        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)
        emit_call_event(call.id, 'hold', {
            'action': 'unhold',
            'path': path,
            'agent': user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True, 'call_id': call.id, 'status': 'active',
            'path': path, 'result': result,
        }), 200
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
@require_permission('can_control_recording')
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


@call_control_bp.route('/<call_id>/record/status', methods=['GET'])
@require_auth
def recording_status(call_id):
    """Return whether this call currently has an active manual recording.

    Reports the presence of the `recording:{call_id}` Redis key set by
    start_recording. Does not know about default SWML-level recording — the
    UI treats absence of a key as "not under manual control."
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    redis_client = get_redis_client()
    control_id = redis_client.get(f'recording:{call.id}') if redis_client else None
    return jsonify({
        'active': bool(control_id),
        'control_id': control_id,
        'recording_url': call.recording_url,
    }), 200


@call_control_bp.route('/<call_id>/record/stop', methods=['POST'])
@require_auth
@require_permission('can_control_recording')
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


# ==================== Live Translate Endpoints ====================

@call_control_bp.route('/<call_id>/translate/start', methods=['POST'])
@require_auth
def start_translate(call_id):
    """Start (or change) bidirectional live_translate on a call's customer leg.

    Body: { "from_lang": "es-ES", "to_lang": "en-US" }
    If translation is already active, this updates the language pair without restarting.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    data = request.get_json() or {}
    from_lang = (data.get('from_lang') or call.caller_language or 'en-US').strip()
    to_lang = (data.get('to_lang') or 'en-US').strip()

    if from_lang == to_lang:
        return jsonify({'error': 'from_lang and to_lang must differ'}), 400

    redis_client = get_redis_client()
    already_active = redis_client.get(f'translate:{call.id}') if redis_client else None

    try:
        sw_api = get_signalwire_api()
        if already_active:
            # live_translate has no `update` action per the SWML docs — only
            # start, stop, summarize, inject. To change languages mid-call we
            # stop the existing session and start a fresh one with the new pair.
            # Brief audio gap is acceptable; a silent no-op from an invented
            # `update` action is not.
            try:
                sw_api.stop_live_translate(call.signalwire_call_sid)
            except Exception as stop_err:
                # If stop fails because no session actually exists server-side
                # (Redis out of sync), log and proceed — start will error clearly
                # if the session is genuinely alive.
                logger.warning(
                    f"stop_live_translate before restart failed on call {call.id}: {stop_err}"
                )
            action = 'updated'
        else:
            action = 'started'

        result = sw_api.start_live_translate(
            call.signalwire_call_sid,
            from_lang=from_lang,
            to_lang=to_lang,
        )

        # Mark translation as on so the UI + future toggles know the state
        if redis_client:
            redis_client.setex(f'translate:{call.id}',
                               7200,
                               json.dumps({'from_lang': from_lang, 'to_lang': to_lang}))

        # Persist on the Call so other agents (takeovers, supervisors) see it
        call.caller_language = from_lang
        call.needs_translation = True
        db.session.commit()

        emit_call_event(call.id, 'translate', {
            'action': action,
            'from_lang': from_lang,
            'to_lang': to_lang,
            'agent': request.current_user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'call_id': call.id,
            'action': action,
            'from_lang': from_lang,
            'to_lang': to_lang,
            'result': result,
        }), 200
    except Exception as e:
        logger.error(f"Failed to start translate for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/translate/stop', methods=['POST'])
@require_auth
def stop_translate(call_id):
    """Stop live_translate on a call's customer leg."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    try:
        sw_api = get_signalwire_api()
        result = sw_api.stop_live_translate(call.signalwire_call_sid)

        redis_client = get_redis_client()
        if redis_client:
            redis_client.delete(f'translate:{call.id}')

        call.needs_translation = False
        db.session.commit()

        emit_call_event(call.id, 'translate', {
            'action': 'stopped',
            'agent': request.current_user.email,
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to stop translate for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/translate/status', methods=['GET'])
@require_auth
def translate_status(call_id):
    """Get current translation state for a call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    redis_client = get_redis_client()
    raw = redis_client.get(f'translate:{call.id}') if redis_client else None
    state = json.loads(raw) if raw else None

    return jsonify({
        'active': state is not None,
        'from_lang': state.get('from_lang') if state else None,
        'to_lang': state.get('to_lang') if state else None,
        'caller_language': call.caller_language,
        'needs_translation': call.needs_translation,
    }), 200


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
def start_monitor(call_id):
    """Start monitoring an active call (audio tap or silent conference join).

    For AI calls (non-conference): uses SignalWire tap to stream audio via WebSocket.
    For human calls (conference-based): prepares a silent conference join.

    Permission: `can_listen_ai_calls` or `can_listen_human_calls` depending on
    who is handling the call. Agents must never be able to silently observe
    arbitrary calls they're not on; gated here before any SignalWire RPC.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user

    # Pick the right permission based on what kind of call this is.
    # A human-handled conference call requires `can_listen_human_calls`;
    # anything else (AI-driven, tap-based) requires `can_listen_ai_calls`.
    is_human_call = bool(call.conference_name and call.handler_type == 'human')
    required_flag = 'can_listen_human_calls' if is_human_call else 'can_listen_ai_calls'
    if not user.has_permission(required_flag):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': [required_flag],
            'missing_permissions': [required_flag],
            'call_type': 'human' if is_human_call else 'ai',
        }), 403

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
def stop_monitor(call_id):
    """Stop monitoring an active call.

    Permission-wise this is the tear-down half of start_monitor. We allow it
    whenever the user has EITHER listen permission — if they got in, they
    must be able to get out even if their role was narrowed mid-session.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user
    if not (user.has_permission('can_listen_ai_calls')
            or user.has_permission('can_listen_human_calls')):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': ['can_listen_ai_calls', 'can_listen_human_calls'],
        }), 403

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

        # Match backup agent to the original caller's language when known
        agent_languages = queue_service.get_languages_for_agents(available)

        selected_agent_id = queue_service.select_agent(
            queue_slug, strategy, available, skill_levels,
            caller_language=call.caller_language,
            agent_languages=agent_languages,
        )

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

    # Whisper mode = supervisor speaks one-way to the requesting agent.
    # Gated by the requesting user's `can_whisper` flag (it's the user opting
    # into the coach-audio shape). Non-whisper escalation = full participant
    # supervisor join and remains available to any authenticated agent.
    if whisper_mode and not request.current_user.has_permission('can_whisper'):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': ['can_whisper'],
            'missing_permissions': ['can_whisper'],
        }), 403

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
