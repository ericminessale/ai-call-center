from flask import request, jsonify
from app import db, socketio, redis_client
from app.api import calls_bp
from app.models import Call, CallLeg, Transcription
from app.services.signalwire_api import get_signalwire_api
from app.utils.decorators import require_auth, validate_json
from app.utils.demo_config import block_in_demo_mode, is_demo_mode
from app.utils.moderation import is_text_acceptable
from app.utils.url_utils import get_base_url, signed_webhook_url
from app.services.queue_service import QueueService
from datetime import datetime, timedelta
from sqlalchemy import func
import logging
import secrets
import json
import os

logger = logging.getLogger(__name__)


@calls_bp.route('/initiate', methods=['POST'])
@require_auth
@block_in_demo_mode
@validate_json('destination', 'destination_type')
def initiate_call():
    """Initiate a new outbound call.

    Soft-blocked in DEMO_MODE — visitors see the dial form but submit
    returns 403 with code 'demo_blocked' so the UI can render a clear
    "not available in demo" toast.
    """
    logger.info("INITIATE CALL REQUEST")
    try:
        data = request.get_json()
        destination = data.get('destination')
        destination_type = data.get('destination_type')
        auto_transcribe = data.get('auto_transcribe', False)

        logger.info(f"Call params: dest={destination}, type={destination_type}, auto_transcribe={auto_transcribe}")

        # Validate destination type
        if destination_type not in ['phone', 'sip']:
            return jsonify({'error': 'Invalid destination_type. Must be "phone" or "sip"'}), 400

        # Get SignalWire API client
        sw_api = get_signalwire_api()

        # Always use the initial-call SWML which handles everything
        base_url = get_base_url()
        swml_url = f"{base_url}/api/swml/initial-call"

        # Use our own webhook endpoint for call state events
        status_callback = signed_webhook_url(f"{base_url}/api/webhooks/call-status")

        # Create call via SignalWire API
        logger.info(f"Calling SignalWire API with swml_url={swml_url}, status_callback={status_callback}")
        sw_call = sw_api.create_call(
            to=destination,
            swml_url=swml_url,
            status_callback=status_callback
        )

        # Extract call_id (SignalWire uses call_id, not call_sid like Twilio)
        call_id = sw_call.sid if hasattr(sw_call, 'sid') else str(sw_call.get('call_id', ''))
        logger.info(f"SignalWire returned call_id: {call_id}")
        logger.info(f"Full SignalWire response object: {sw_call.__dict__ if hasattr(sw_call, '__dict__') else sw_call}")

        # Save call to database
        call = Call(
            user_id=request.current_user.id,
            signalwire_call_sid=call_id,  # Despite the column name, this stores call_id
            destination=destination,
            destination_type=destination_type,
            status='initiated',
            transcription_active=True  # Always true now
        )
        db.session.add(call)
        db.session.commit()

        logger.info(f"Call saved to DB with id={call.id}, signalwire_call_sid={call.signalwire_call_sid}")

        # Emit call initiated event
        socketio.emit('call_initiated', {
            'call_sid': call_id,  # Frontend expects call_sid but we send call_id
            'destination': destination,
            'user_id': request.current_user.id
        }, room=request.current_user.id)

        return jsonify({
            'success': True,
            'call_id': call_id,  # This is the SignalWire call_id that should be used for events
            'call_sid': call_id,  # Keep for compatibility (frontend expects call_sid)
            'destination': destination,
            'status': 'initiated'
        }), 201

    except Exception as e:
        logger.error(f"Failed to initiate call: {str(e)}")
        return jsonify({'error': f'Failed to initiate call: {str(e)}'}), 500


@calls_bp.route('/<call_sid>/transcription', methods=['PUT'])
@require_auth
@validate_json('action')
def update_transcription(call_sid):
    """Control transcription for an active call."""
    try:
        data = request.get_json()
        action = data.get('action')

        # Validate action
        if action not in ['start', 'stop', 'summarize']:
            return jsonify({'error': 'Invalid action. Must be "start", "stop", or "summarize"'}), 400

        # Find call
        call = Call.find_by_sid(call_sid)
        if not call:
            return jsonify({'error': 'Call not found'}), 404


        # Get SignalWire API client
        sw_api = get_signalwire_api()

        # Handle different actions using direct API calls
        base_url = get_base_url()

        if action == 'start':
            # Start transcription
            webhook_url = signed_webhook_url(f"{base_url}/api/webhooks/transcription")
            sw_api.start_transcription(call_sid, webhook_url)
        elif action == 'stop':
            # Stop transcription
            sw_api.stop_transcription(call_sid)
        elif action == 'summarize':
            # Request summary
            webhook_url = signed_webhook_url(f"{base_url}/api/webhooks/summary")
            prompt = data.get('prompt', 'Summarize the key points of this conversation.')
            sw_api.summarize_call(call_sid, webhook_url, prompt)

        # Update transcription status in database
        if action == 'start':
            call.transcription_active = True
        elif action == 'stop':
            call.transcription_active = False

        db.session.commit()

        # Emit transcription control event
        socketio.emit('transcription_control', {
            'call_sid': call_sid,
            'action': action
        }, room=call_sid)

        return jsonify({
            'success': True,
            'call_sid': call_sid,
            'action': action,
            'message': f'Transcription {action} successful'
        }), 200

    except Exception as e:
        logger.error(f"Failed to update transcription: {str(e)}")
        return jsonify({'error': f'Failed to update transcription: {str(e)}'}), 500


@calls_bp.route('/<call_id>', methods=['GET'])
@require_auth
def get_call(call_id):
    """Get call details by database ID or SignalWire call_sid."""
    try:
        # Try to find by database ID first (if numeric), then by SignalWire SID
        call = None
        if call_id.isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            logger.error(f"Call not found in database: {call_id}")
            return jsonify({'error': 'Call not found'}), 404


        # Get transcriptions for the call
        transcriptions = Transcription.find_by_call(call.id)

        # Get call dict and add dashboard status
        call_dict = call.to_dict()
        dashboard_status = map_to_dashboard_status(call.status)
        call_dict['dashboard_status'] = dashboard_status

        return jsonify({
            'call': call_dict,
            'transcriptions': [t.to_dict() for t in transcriptions]
        }), 200

    except Exception as e:
        logger.error(f"Failed to get call details: {str(e)}")
        return jsonify({'error': f'Failed to get call details: {str(e)}'}), 500


@calls_bp.route('', methods=['GET'])
@calls_bp.route('/', methods=['GET'])
@require_auth
def list_calls():
    """List all calls for the current user or agent."""
    try:
        # Get pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 100, type=int)  # Increased for dashboard
        search = request.args.get('search', '').strip()

        # Get status filters (can be multiple)
        status_filters = request.args.getlist('status')  # e.g., ?status=waiting&status=ai_active
        agent_id = request.args.get('agent_id')  # Filter by assigned agent

        # Query calls for the user
        from app import db
        from app.models.transcription import Transcription

        # Map dashboard status names to our internal statuses
        status_mapping = {
            'waiting': ['created', 'ringing'],
            'ai_active': ['ai_active'],  # AI calls have explicit ai_active status
            'active': ['answered'],
            'completed': ['ended', 'completed']
        }

        # For AI active calls, show all calls to all agents (no user_id filter)
        # For other calls, only show user's own calls
        if status_filters and 'ai_active' in status_filters:
            # AI calls are visible to all agents - no user_id filter
            query = db.session.query(Call)
        else:
            # User's own calls only
            query = db.session.query(Call).filter_by(user_id=request.current_user.id)

        # Apply status filters if provided
        if status_filters:
            internal_statuses = []
            for status in status_filters:
                if status in status_mapping:
                    internal_statuses.extend(status_mapping[status])
                else:
                    internal_statuses.append(status)

            if internal_statuses:
                query = query.filter(Call.status.in_(internal_statuses))

        # Filter by agent if provided
        if agent_id:
            # TODO: Add agent_id column to Call model
            # query = query.filter(Call.agent_id == agent_id)
            pass

        # Add search functionality
        if search:
            # Search in destination, status, summary, and transcription content
            query = query.outerjoin(Transcription).filter(
                db.or_(
                    Call.destination.ilike(f'%{search}%'),
                    Call.status.ilike(f'%{search}%'),
                    Call.summary.ilike(f'%{search}%'),
                    Transcription.transcript.ilike(f'%{search}%')
                )
            ).distinct()

        calls = query.order_by(Call.created_at.desc()) \
                    .paginate(page=page, per_page=per_page, error_out=False)

        # Prepare call data with transcription content
        calls_data = []
        for call in calls.items:
            call_dict = call.to_dict()

            # Map internal status to dashboard status
            dashboard_status = map_to_dashboard_status(call.status)
            call_dict['dashboard_status'] = dashboard_status

            # Add full transcript for search purposes
            if call.transcriptions:
                full_transcript = Transcription.get_full_transcript(call.id)
                call_dict['full_transcript'] = full_transcript

                # Get transcription messages for display
                transcriptions = Transcription.find_by_call(call.id)
                call_dict['transcription'] = [
                    {
                        'speaker': t.speaker or 'unknown',
                        'text': t.transcript,
                        'timestamp': t.created_at.isoformat() if t.created_at else None
                    }
                    for t in transcriptions
                ]
            else:
                call_dict['full_transcript'] = ''
                call_dict['transcription'] = []

            calls_data.append(call_dict)

        return jsonify({
            'calls': calls_data,
            'total': calls.total,
            'page': page,
            'per_page': per_page,
            'pages': calls.pages
        }), 200

    except Exception as e:
        logger.error(f"Failed to list calls: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to list calls: {str(e)}'}), 500


def map_to_dashboard_status(internal_status):
    """Map internal call status to dashboard status."""
    status_map = {
        'created': 'waiting',
        'ringing': 'waiting',
        'initiated': 'waiting',
        'answered': 'ai_active',  # TODO: Distinguish AI vs human based on call routing
        'ended': 'completed',
        'completed': 'completed'
    }
    return status_map.get(internal_status, internal_status)


@calls_bp.route('/<call_id>/end', methods=['POST'])
@require_auth
def end_call(call_id):
    """End an active call by database ID or SignalWire call_sid."""
    logger.info(f"END CALL REQUEST: call_id={call_id}")
    logger.info(f"Current user: {request.current_user.id if request.current_user else 'None'}")

    try:
        # Try to find by database ID first (if numeric), then by SignalWire SID
        call = None
        if call_id.isdigit():
            # Numeric ID - try database lookup first
            call = db.session.query(Call).filter_by(id=int(call_id)).first()

        if not call:
            # Try by SignalWire call SID (handles "call-xxxxx" format)
            call = Call.find_by_sid(call_id)
        logger.info(f"Found call in DB: {call.to_dict() if call else 'NOT FOUND'}")

        if not call:
            logger.error(f"Call not found in database: {call_id}")
            return jsonify({'error': 'Call not found'}), 404


        logger.info(f"Attempting to end call via SignalWire API: {call.signalwire_call_sid}")

        # Try to end via SignalWire API, but don't fail if call already ended on their side
        sw_api_error = None
        try:
            sw_api = get_signalwire_api()
            result = sw_api.end_call(call.signalwire_call_sid)
            logger.info(f"SignalWire API response: {result}")
        except Exception as sw_err:
            # Call may already be ended on SignalWire's side — that's OK, still update our state
            sw_api_error = str(sw_err)
            logger.warning(f"SignalWire end_call failed (call may already be ended): {sw_api_error}")

        # Always update call status and emit events regardless of SignalWire API result
        call.update_status('completed')
        call.ended_at = call.ended_at or datetime.utcnow()
        if call.answered_at and not call.duration:
            call.duration = int((call.ended_at - call.answered_at).total_seconds())
        db.session.commit()
        logger.info(f"Call status updated to 'completed' in database")

        # Emit call update so frontend removes from active list
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        # Also emit call_ended for comprehensive UI cleanup (matches webhook pattern)
        from app import socketio
        call_ended_data = {
            'callId': call.id,
            'call_sid': call.signalwire_call_sid,
            'conference_name': call.conference_name,
            'reset_ui': True
        }
        if call.user_id:
            socketio.emit('call_ended', call_ended_data, room=str(call.user_id))
        socketio.emit('call_ended', call_ended_data)

        return jsonify({
            'success': True,
            'call_id': call.id,
            'call_sid': call.signalwire_call_sid,
            'message': 'Call ended successfully' if not sw_api_error else 'Call marked as ended (was already completed on SignalWire)'
        }), 200

    except Exception as e:
        logger.error(f"Failed to end call: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': f'Failed to end call: {str(e)}'}), 500


@calls_bp.route('/<call_sid>/transcript', methods=['GET'])
@require_auth
def get_full_transcript(call_sid):
    """Get the complete transcript for a call."""
    try:
        # Find call
        call = Call.find_by_sid(call_sid)
        if not call:
            return jsonify({'error': 'Call not found'}), 404


        # Get full transcript
        transcript = Transcription.get_full_transcript(call.id)

        # Get summary if exists
        from app import db
        summary_record = db.session.query(Transcription).filter_by(
            call_id=call.id
        ).filter(Transcription.summary.isnot(None)).first()

        return jsonify({
            'call_sid': call_sid,
            'transcript': transcript,
            'summary': summary_record.to_dict() if summary_record else None
        }), 200

    except Exception as e:
        logger.error(f"Failed to get transcript: {str(e)}")
        return jsonify({'error': f'Failed to get transcript: {str(e)}'}), 500


@calls_bp.route('/<call_id>/ai-message', methods=['POST'])
@require_auth
@validate_json('message')
def send_ai_message(call_id):
    """Send a system message to an active AI agent during a call by database ID or SignalWire call_sid.

    This allows agents/supervisors to guide the AI's behavior in real-time.

    Request body:
    {
        "message": "Offer the customer a 20% discount",
        "role": "system"  // optional, defaults to "system"
    }
    """
    logger.info(f"AI MESSAGE REQUEST for call {call_id}")
    try:
        data = request.get_json()
        message_text = data.get('message')
        role = data.get('role', 'system')

        # Hosted-demo content moderation: visitor types this text and
        # the AI agent immediately speaks/acts on it. A slur or threat
        # would be heard by anyone on the call. Reject before it
        # reaches the AI.
        if is_demo_mode():
            ok, reason = is_text_acceptable(message_text)
            if not ok:
                return jsonify({
                    'error': reason,
                    'code': 'moderation_blocked',
                    'field': 'message',
                }), 422

        # Try to find by database ID first (if numeric), then by SignalWire SID
        call = None
        if call_id.isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        # Use the resolved SignalWire SID from the call record, not the
        # caller-supplied identifier (which may be a numeric DB id).
        call_sid = call.signalwire_call_sid
        logger.info(f"Sending AI message to call {call_sid}: role={role}, message={message_text}")

        # Get SignalWire API and send message
        sw_api = get_signalwire_api()
        result = sw_api.send_ai_message(call_sid, message_text, role)

        logger.info(f"AI message sent successfully to call {call_sid}")

        return jsonify({
            'success': True,
            'call_sid': call_sid,
            'message': message_text,
            'role': role,
            'result': result
        }), 200

    except Exception as e:
        logger.error(f"Failed to send AI message: {str(e)}")
        return jsonify({'error': f'Failed to send AI message: {str(e)}'}), 500


@calls_bp.route('/<call_db_id>/register-ai-leg', methods=['POST'])
def register_ai_leg(call_db_id):
    """Register the AI agent's B-leg call SID for later use (e.g., takeover).

    Called by the AI agent's capture_base_url callback when it starts handling a call.
    No auth required - called internally from ai-agents container.

    Request body:
    {
        "signalwire_sid": "call-xxxxx"  // The B-leg's call SID
    }
    """
    try:
        data = request.get_json() or {}
        signalwire_sid = data.get('signalwire_sid')

        if not signalwire_sid:
            return jsonify({'error': 'signalwire_sid is required'}), 400

        # Find the active AI leg for this call
        call_id = int(call_db_id)
        ai_leg = CallLeg.query.filter_by(
            call_id=call_id,
            leg_type='ai_agent',
            status='active'
        ).first()

        if ai_leg:
            ai_leg.signalwire_sid = signalwire_sid
            db.session.commit()
            logger.info(f"Registered AI leg SID {signalwire_sid} for call {call_id} (leg {ai_leg.id})")
        else:
            logger.warning(f"No active AI leg found for call {call_id} to register SID {signalwire_sid}")

        return jsonify({'success': True}), 200

    except Exception as e:
        logger.error(f"Failed to register AI leg: {str(e)}")
        return jsonify({'error': str(e)}), 500


@calls_bp.route('/<call_db_id>/sentiment', methods=['POST'])
def report_sentiment(call_db_id):
    """Receive real-time sentiment updates from AI agents during a call.

    Called by the AI agent's report_sentiment SWAIG tool (fire-and-forget).
    No auth required - called internally from ai-agents container.

    Request body:
    {
        "score": 0.7,         // -1.0 to 1.0
        "reason": "Customer expressed satisfaction with resolution"
    }
    """
    try:
        data = request.get_json() or {}
        score = data.get('score')
        reason = data.get('reason', '')

        if score is None:
            return jsonify({'error': 'score is required'}), 400

        # Clamp to valid range
        score = max(-1.0, min(1.0, float(score)))

        call_id = int(call_db_id)
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        # Update the call's sentiment score
        call.sentiment_score = score
        db.session.commit()

        logger.info(f"Sentiment update for call {call_id}: {score} ({reason})")

        # Emit real-time socket event so the UI updates immediately
        sentiment_data = {
            'callId': call_id,
            'score': score,
            'reason': reason,
            'timestamp': datetime.utcnow().isoformat(),
        }

        # Broadcast to anyone watching this call + the assigned agent
        socketio.emit('sentiment_update', sentiment_data)

        # Also update the contact's average sentiment if linked
        if call.contact_id:
            from app.models import Contact
            contact = Contact.query.get(call.contact_id)
            if contact:
                # Average sentiment across all completed calls — treat no-sentiment calls as 0 (neutral)
                from sqlalchemy import func, case
                avg = db.session.query(
                    func.avg(case((Call.sentiment_score.isnot(None), Call.sentiment_score), else_=0))
                ).filter(
                    Call.contact_id == call.contact_id,
                    Call.status.in_(['ended', 'completed'])
                ).scalar()
                if avg is not None:
                    contact.average_sentiment = round(float(avg), 2)
                    db.session.commit()

        return jsonify({'success': True}), 200

    except Exception as e:
        logger.error(f"Failed to process sentiment update: {str(e)}")
        return jsonify({'error': str(e)}), 500


@calls_bp.route('/<call_sid>/takeover', methods=['POST'])
@require_auth
def initiate_takeover(call_sid):
    """Initiate a takeover of an AI-active call by a human agent.

    Generates a SWML URL that the agent's Call Fabric client will dial.
    The SWML uses execute_rpc to end the AI and connect to call:{sid}.

    Returns:
    {
        "dial_address": "/public/agent-conference-swml?token=xxx",
        "call_sid": "call-xxxxx",
        "call_id": 123,
        "leg_id": 456
    }
    """
    logger.info(f"TAKEOVER REQUEST for call {call_sid} by user {request.current_user.id}")

    try:
        # Find the call by SignalWire call_sid
        call = Call.find_by_sid(call_sid)
        if not call:
            logger.error(f"Call not found: {call_sid}")
            return jsonify({'error': 'Call not found'}), 404

        # Validate call is currently AI-handled
        if call.handler_type != 'ai':
            logger.warning(f"Call {call_sid} is not AI-handled (handler_type={call.handler_type})")
            return jsonify({'error': 'Call is not currently handled by AI'}), 400

        # Validate call is active
        if call.status not in ['ai_active', 'answered', 'ringing']:
            logger.warning(f"Call {call_sid} is not active (status={call.status})")
            return jsonify({'error': 'Call is not active'}), 400

        # End current AI leg and create new human leg
        new_leg = CallLeg.create_next_leg(
            call=call,
            leg_type='human_agent',
            user_id=request.current_user.id
        )
        db.session.commit()

        logger.info(f"Created new human leg {new_leg.id} for call {call.id}")

        # Generate secure takeover token
        token = secrets.token_urlsafe(32)

        # Store takeover info in Redis — the conference webhook will check for
        # type='takeover' and return SWML that connects agent to call:{sid}
        takeover_data = json.dumps({
            'type': 'takeover',
            'agent_id': request.current_user.id,
            'call_sid': call.signalwire_call_sid,
            'call_id': call.id,
            'leg_id': new_leg.id,
            'user_id': request.current_user.id
        })
        redis_client.setex(f'conference_join:{token}', 120, takeover_data)

        logger.info(f"Stored takeover token in Redis: {token[:8]}...")

        # Build dial address with token — same resource as conference join
        resource_address = os.getenv('AGENT_CONFERENCE_RESOURCE', '/public/agent-conference-swml')
        dial_address = f"{resource_address}?token={token}"

        # Update call handler type to human (takeover in progress)
        call.handler_type = 'human'
        call.user_id = request.current_user.id
        db.session.commit()

        # Emit event to notify UI
        socketio.emit('call_takeover_initiated', {
            'call_sid': call.signalwire_call_sid,
            'call_id': call.id,
            'agent_id': request.current_user.id,
            'leg_id': new_leg.id
        }, room=f'call_{call.signalwire_call_sid}')

        return jsonify({
            'success': True,
            'dial_address': dial_address,
            'call_sid': call.signalwire_call_sid,
            'call_id': call.id,
            'leg_id': new_leg.id
        }), 200

    except Exception as e:
        logger.error(f"Failed to initiate takeover: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to initiate takeover: {str(e)}'}), 500


@calls_bp.route('/<call_id>/take', methods=['POST'])
@require_auth
def take_queued_call(call_id):
    """Take a queued call.

    This endpoint allows an agent to take a call from the queue.
    If the call is already assigned to this agent, it returns success.
    If the call is waiting, it assigns it to this agent.

    Returns the conference info so the agent can dial in.
    """
    logger.info(f"TAKE CALL REQUEST for call {call_id} by user {request.current_user.id}")

    try:
        # Find call by ID
        call = None
        if str(call_id).isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            logger.error(f"Call not found: {call_id}")
            return jsonify({'error': 'Call not found'}), 404

        # Check if call is in a takeable state
        # Include 'ai_active' and 'answered' so agents can take calls while AI is still handling
        takeable_statuses = ['waiting', 'assigned', 'queued', 'ai_active', 'answered']
        if call.status not in takeable_statuses:
            logger.warning(f"Call {call_id} cannot be taken (status={call.status})")
            return jsonify({'error': f'Call cannot be taken (status: {call.status})'}), 400

        # Check if already assigned to another agent
        if call.status == 'assigned' and call.assigned_agent_id and call.assigned_agent_id != request.current_user.id:
            logger.warning(f"Call {call_id} is assigned to another agent ({call.assigned_agent_id})")
            return jsonify({'error': 'Call is assigned to another agent'}), 409

        # Assign to this agent if not already
        if call.assigned_agent_id != request.current_user.id:
            call.assigned_agent_id = request.current_user.id
            call.assigned_at = datetime.utcnow()

        call.status = 'assigned'
        call.handler_type = 'human'
        call.user_id = request.current_user.id

        # Ensure conference name is set
        if not call.conference_name:
            call.conference_name = f"interaction-{call.signalwire_call_sid}"

        db.session.commit()

        logger.info(f"Call {call_id} taken by agent {request.current_user.id}, conference: {call.conference_name}")

        # Emit queue update to remove from other agents' queue displays
        from app import socketio
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': call.queue_id,
            'action': 'taken',
            'taken_by_agent_id': request.current_user.id
        })

        return jsonify({
            'success': True,
            'call_id': call.id,
            'call_sid': call.signalwire_call_sid,
            'conference_name': call.conference_name,
            'message': 'Call assigned successfully'
        }), 200

    except Exception as e:
        logger.error(f"Failed to take call: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to take call: {str(e)}'}), 500


@calls_bp.route('/<call_id>/status', methods=['PUT'])
@require_auth
def update_call_status(call_id):
    """Update call status.

    Called by the frontend when agent joins/leaves the conference.
    This keeps the call status in sync with the actual call state.
    """
    logger.info(f"STATUS UPDATE for call {call_id} by user {request.current_user.id}")

    try:
        data = request.get_json() or {}
        new_status = data.get('status')

        if not new_status:
            return jsonify({'error': 'status is required'}), 400

        # Validate status
        valid_statuses = ['active', 'on_hold', 'ended', 'waiting', 'assigned']
        if new_status not in valid_statuses:
            return jsonify({'error': f'Invalid status: {new_status}'}), 400

        # Find call by ID
        call = None
        if str(call_id).isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            logger.error(f"Call not found: {call_id}")
            return jsonify({'error': 'Call not found'}), 404

        old_status = call.status

        # Update status
        call.status = new_status
        call.handler_type = 'human'  # Agent is now handling

        # If becoming active, mark answered time
        if new_status == 'active' and not call.answered_at:
            call.answered_at = datetime.utcnow()

        # If ended, mark ended time
        if new_status == 'ended' and not call.ended_at:
            call.ended_at = datetime.utcnow()
            if call.answered_at:
                call.duration = int((call.ended_at - call.answered_at).total_seconds())

        db.session.commit()
        logger.info(f"Call {call_id} status updated: {old_status} -> {new_status}")

        # Emit update to other clients
        from app import socketio
        socketio.emit('call_update', {
            'call': call.to_dict(include_contact=True)
        })

        return jsonify({
            'success': True,
            'call_id': call.id,
            'status': call.status
        }), 200

    except Exception as e:
        logger.error(f"Failed to update call status: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to update call status: {str(e)}'}), 500


@calls_bp.route('/<call_id>/legs', methods=['GET'])
@require_auth
def get_call_legs(call_id):
    """Get all legs for a call."""
    try:
        # Find call by ID or SID
        call = None
        if call_id.isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        # Get all legs
        legs = CallLeg.get_legs_for_call(call.id)

        return jsonify({
            'call_id': call.id,
            'call_sid': call.signalwire_call_sid,
            'legs': [leg.to_dict() for leg in legs]
        }), 200

    except Exception as e:
        logger.error(f"Failed to get call legs: {str(e)}")
        return jsonify({'error': f'Failed to get call legs: {str(e)}'}), 500


# =============================================================================
# Call Wrap-up (Tier 2a)
# =============================================================================

# Disposition codes — short slugs the agent picks during wrap-up. Kept as a
# Python constant for v1 so the list is reviewable in code; admin-configurable
# storage can come later via system_config without breaking this contract.
DISPOSITION_CODES = [
    {'code': 'resolved',           'label': 'Resolved',             'description': "Caller's issue was handled."},
    {'code': 'transferred',        'label': 'Transferred',          'description': 'Routed to another agent or department.'},
    {'code': 'callback-scheduled', 'label': 'Callback scheduled',   'description': 'Will call the contact back later.'},
    {'code': 'escalated',          'label': 'Escalated',            'description': 'Passed up to a supervisor.'},
    {'code': 'sales-opportunity',  'label': 'Sales opportunity',    'description': 'Lead worth following up on.'},
    {'code': 'technical-issue',    'label': 'Technical issue',      'description': 'Could not resolve due to a technical limitation.'},
    {'code': 'no-answer',          'label': 'No answer / voicemail', 'description': 'Outbound only — call went unanswered or to voicemail.'},
    {'code': 'wrong-number',       'label': 'Wrong number',         'description': 'Misrouted or invalid contact.'},
    {'code': 'spam',               'label': 'Spam / robocall',      'description': 'Unsolicited or automated.'},
    {'code': 'abandoned',          'label': 'Abandoned',            'description': 'Caller dropped before resolution.'},
    {'code': 'other',              'label': 'Other',                'description': 'Something else — see notes.'},
]
DISPOSITION_CODE_SET = {d['code'] for d in DISPOSITION_CODES}


@calls_bp.route('/dispositions', methods=['GET'])
@require_auth
def list_dispositions():
    """Return the disposition code dictionary for the wrap-up dropdown."""
    return jsonify({'dispositions': DISPOSITION_CODES}), 200


@calls_bp.route('/<call_id>/wrap-up', methods=['PUT'])
@require_auth
def update_wrap_up(call_id):
    """Save the agent's wrap-up — disposition code and / or notes.

    Either field is optional individually; sending neither is a no-op
    (still returns 200 so a debounced UI doesn't churn). The first time
    *anything* is saved we stamp `wrapped_up_at`; subsequent edits update
    the values but leave the original timestamp alone so reporting can
    answer "when was wrap-up first completed."
    """
    try:
        data = request.get_json() or {}
        disposition_code = data.get('disposition_code')
        agent_notes = data.get('agent_notes')

        # Validate disposition early — reject anything we don't recognise so
        # we don't pollute reporting with typos. None / empty string clears.
        if disposition_code is not None and disposition_code != '' \
                and disposition_code not in DISPOSITION_CODE_SET:
            return jsonify({
                'error': f'Unknown disposition code: {disposition_code}',
                'valid_codes': sorted(DISPOSITION_CODE_SET),
            }), 400

        # Notes have a generous size cap to prevent abuse / accidents.
        if agent_notes is not None and len(agent_notes) > 5000:
            return jsonify({'error': 'agent_notes must be 5000 characters or fewer'}), 400

        # Look up by numeric ID first, then SignalWire call_sid.
        call = None
        if str(call_id).isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        changed = False
        if 'disposition_code' in data:
            new_disp = disposition_code or None  # treat empty string as clear
            if call.disposition_code != new_disp:
                call.disposition_code = new_disp
                changed = True
        if 'agent_notes' in data:
            new_notes = agent_notes or None
            if call.agent_notes != new_notes:
                call.agent_notes = new_notes
                changed = True

        if changed and not call.wrapped_up_at:
            call.wrapped_up_at = datetime.utcnow()

        if changed:
            db.session.commit()
            logger.info(
                "Wrap-up saved for call %s: disposition=%s notes_len=%s",
                call.id,
                call.disposition_code,
                len(call.agent_notes) if call.agent_notes else 0,
            )

            # Notify other clients viewing this contact / call so the panel updates live.
            from app.services.callcenter_socketio import emit_call_update
            emit_call_update(call)

        return jsonify({
            'success': True,
            'call': {
                'id': call.id,
                'disposition_code': call.disposition_code,
                'agent_notes': call.agent_notes,
                'wrapped_up_at': call.wrapped_up_at.isoformat() if call.wrapped_up_at else None,
            },
        }), 200

    except Exception as e:
        logger.error(f"Failed to save wrap-up: {str(e)}")
        return jsonify({'error': f'Failed to save wrap-up: {str(e)}'}), 500


@calls_bp.route('/cleanup-stale', methods=['POST'])
@require_auth
def cleanup_stale_calls():
    """Clean up stale calls that are stuck in ringing/active status.

    Marks calls as 'ended' if they've been in ringing/active status for too long.
    This handles cases where webhooks didn't fire properly.

    Query params:
    - force=true: Clean ALL non-terminal calls regardless of age (for dev)
    - max_age_minutes=N: Override the default 60 minute threshold
    """
    try:
        force = request.args.get('force', 'false').lower() == 'true'
        max_age_minutes = request.args.get('max_age_minutes', 60, type=int)

        if force:
            # Clean ALL non-terminal calls
            stale_calls = db.session.query(Call).filter(
                Call.status.in_(['ringing', 'active', 'connecting', 'ai_active'])
            ).all()
        else:
            # Find calls stuck in non-terminal states for more than max_age_minutes
            cutoff_time = datetime.utcnow() - timedelta(minutes=max_age_minutes)
            stale_calls = db.session.query(Call).filter(
                Call.status.in_(['ringing', 'active', 'connecting', 'ai_active']),
                Call.created_at < cutoff_time
            ).all()

        cleaned_count = 0
        for call in stale_calls:
            logger.info(f"Cleaning up stale call {call.id}: status={call.status}, created={call.created_at}")
            call.status = 'ended'
            call.ended_at = datetime.utcnow()
            cleaned_count += 1

        db.session.commit()

        # Emit updates for cleaned calls
        from app.services.callcenter_socketio import emit_call_update
        for call in stale_calls:
            emit_call_update(call)

        logger.info(f"Cleaned up {cleaned_count} stale calls")

        return jsonify({
            'success': True,
            'cleaned_count': cleaned_count,
            'calls': [{'id': c.id, 'status': c.status} for c in stale_calls]
        }), 200

    except Exception as e:
        logger.error(f"Failed to cleanup stale calls: {str(e)}")
        return jsonify({'error': f'Failed to cleanup stale calls: {str(e)}'}), 500


@calls_bp.route('/my-stats', methods=['GET'])
@require_auth
def get_my_stats():
    """Get real-time stats for the current agent."""
    try:
        user_id = request.user_id
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Calls handled today (where this agent was assigned or initiated)
        calls_today = db.session.query(func.count(Call.id)).filter(
            ((Call.assigned_agent_id == user_id) | (Call.user_id == user_id)),
            Call.created_at >= today_start,
            Call.status.in_(['ended', 'completed', 'answered', 'active'])
        ).scalar() or 0

        # Average handle time today (seconds) for completed calls
        avg_handle_time = db.session.query(func.avg(Call.duration)).filter(
            ((Call.assigned_agent_id == user_id) | (Call.user_id == user_id)),
            Call.created_at >= today_start,
            Call.duration.isnot(None)
        ).scalar() or 0

        # Queue depth across all queues
        total_queue_depth = 0
        longest_wait = 0
        try:
            queue_service = QueueService(redis_client)
            waiting_calls = db.session.query(Call).filter(
                Call.status.in_(['waiting', 'queued', 'assigned'])
            ).all()
            total_queue_depth = len(waiting_calls)
            for call in waiting_calls:
                wait = call.wait_time_seconds
                if wait > longest_wait:
                    longest_wait = wait
        except Exception as exc:
            # Redis may not be available — fall back to zero counts so the
            # rest of the dashboard still renders. Worth knowing in logs.
            logger.warning("queue depth lookup failed (Redis unavailable?): %s", exc)

        return jsonify({
            'success': True,
            'stats': {
                'callsToday': calls_today,
                'avgHandleTime': int(avg_handle_time),
                'queueDepth': total_queue_depth,
                'longestWait': longest_wait,
            }
        }), 200

    except Exception as e:
        logger.error(f"Failed to get agent stats: {str(e)}")
        return jsonify({'error': str(e)}), 500