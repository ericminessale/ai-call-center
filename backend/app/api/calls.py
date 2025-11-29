from flask import request, jsonify
from app import db, socketio
from app.api import calls_bp
from app.models import Call, Transcription
from app.services.signalwire_api import get_signalwire_api
from app.utils.decorators import require_auth, validate_json
import logging

logger = logging.getLogger(__name__)


@calls_bp.route('/initiate', methods=['POST'])
@require_auth
@validate_json('destination', 'destination_type')
def initiate_call():
    """Initiate a new outbound call."""
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
        base_url = request.host_url.rstrip('/')
        swml_url = f"{base_url}/api/swml/initial-call"

        # Use our own webhook endpoint for call state events
        status_callback = f"{base_url}/api/webhooks/call-status"

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
        base_url = request.host_url.rstrip('/')

        if action == 'start':
            # Start transcription
            webhook_url = f"{base_url}/api/webhooks/transcription"
            sw_api.start_transcription(call_sid, webhook_url)
        elif action == 'stop':
            # Stop transcription
            sw_api.stop_transcription(call_sid)
        elif action == 'summarize':
            # Request summary
            webhook_url = f"{base_url}/api/webhooks/summary"
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

        # Get SignalWire API and end the call using SignalWire SID
        sw_api = get_signalwire_api()
        result = sw_api.end_call(call.signalwire_call_sid)

        logger.info(f"SignalWire API response: {result}")

        # Update call status
        call.update_status('completed')
        db.session.commit()
        logger.info(f"Call status updated to 'completed' in database")

        # Don't emit call_ended here - the webhook will handle it when SignalWire confirms the call ended
        # This prevents duplicate call_ended events

        return jsonify({
            'success': True,
            'call_id': call.id,
            'call_sid': call.signalwire_call_sid,
            'message': 'Call ended successfully'
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

        # Try to find by database ID first (if numeric), then by SignalWire SID
        call = None
        if call_id.isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        logger.info(f"Sending AI message to call {call_id}: role={role}, message={message_text}")

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