from flask import Blueprint, request, jsonify, Response, make_response
from app import db, redis_client
from app.models import Conference, ConferenceParticipant, Call, CallLeg, User
from app.services.callcenter_socketio import emit_call_update
from app.utils.decorators import require_auth
from app.utils.url_utils import get_base_url
import logging
import json
import os
import re
import uuid
from datetime import datetime
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

conferences_bp = Blueprint('conferences', __name__)


# ============================================================================
# API Endpoints (require auth)
# ============================================================================

@conferences_bp.route('/prepare-join', methods=['POST'])
@require_auth
def prepare_conference_join():
    """Prepare a conference join by storing params in Redis.

    This endpoint is called by the frontend BEFORE dialing the conference resource.
    It stores the agent_id and conference_name in Redis with a unique token,
    then returns the token. The frontend includes just the token in the dial address,
    and the webhook looks up the params from Redis.

    This approach is more reliable than relying on SignalWire to forward query params.

    Request body:
    {
        "agent_id": 4,
        "conference_name": "interaction-xxx",
        "call_id": "123"  // Optional: database call ID
    }

    Response:
    {
        "token": "abc123...",
        "dial_address": "/public/agent-conference-swml?token=abc123..."
    }
    """
    data = request.get_json() or {}

    agent_id = data.get('agent_id')
    conference_name = data.get('conference_name')
    call_id = data.get('call_id')

    if not agent_id:
        return jsonify({'error': 'agent_id is required'}), 400

    if not conference_name:
        return jsonify({'error': 'conference_name is required'}), 400

    # Generate a unique token
    token = str(uuid.uuid4())

    # Store params in Redis with 5-minute TTL (should only take seconds to use)
    redis_key = f"conference_join:{token}"
    redis_data = json.dumps({
        'agent_id': agent_id,
        'conf': conference_name,
        'call_id': call_id
    })
    redis_client.setex(redis_key, 300, redis_data)  # 5 minute TTL

    logger.info(f"Prepared conference join: token={token}, agent={agent_id}, conf={conference_name}")

    # Build the dial address with just the token
    resource_address = os.getenv('AGENT_CONFERENCE_RESOURCE', '/public/agent-conference-swml')
    dial_address = f"{resource_address}?token={token}"

    return jsonify({
        'token': token,
        'dial_address': dial_address,
        'conference_name': conference_name
    })


# ============================================================================
# CXML/SWML Webhook Endpoints (called by SignalWire, no auth required)
# ============================================================================

@conferences_bp.route('/agent-conference', methods=['POST', 'GET'])
def agent_conference_webhook():
    """SWML webhook endpoint for agent conference join.

    This endpoint handles TWO modes:
    1. Per-interaction conferences (NEW): If 'conf' param provided, join that specific conference
    2. Per-agent conferences (LEGACY): If no 'conf', create/join agent's personal conference

    Setup in SignalWire Dashboard:
    1. Go to Resources > Add New > Script > SWML Script (or CXML Script)
    2. Set Request URL to: https://your-ngrok.io/api/conferences/agent-conference
    3. Note the assigned address (e.g., /public/agent-conference)
    4. Set AGENT_CONFERENCE_RESOURCE env var to that address

    Usage:
    - Per-interaction: dial('/public/agent-conference?conf=interaction-abc123&agent_id=4')
    - Per-agent (legacy): dial('/public/agent-conference?agent_id=4')
    """
    logger.info(f"Agent conference webhook called")
    logger.info(f"Request method: {request.method}")
    logger.info(f"Request URL: {request.url}")
    logger.info(f"Request headers: {dict(request.headers)}")
    logger.info(f"Request args: {dict(request.args)}")
    logger.info(f"Request form: {dict(request.form)}")
    logger.info(f"Request content type: {request.content_type}")

    # Log raw data for debugging
    try:
        raw_data = request.get_data(as_text=True)
        logger.info(f"Request raw data: {raw_data[:2000] if raw_data else 'empty'}")
    except Exception as e:
        logger.info(f"Could not get raw data: {e}")

    # Try to get JSON body if present
    json_data = {}
    if request.is_json:
        json_data = request.get_json() or {}
        logger.info(f"Request JSON: {json_data}")

    # Parse query params from multiple sources
    parsed_params = {}

    # Source 1: URL query params (request.args)
    # Source 2: Form data 'To' or 'Called' field (may contain query string)
    to_param = request.form.get('To', '') or request.form.get('Called', '') or request.form.get('to', '') or request.form.get('called', '')

    if '?' in to_param:
        query_string = to_param.split('?', 1)[1]
        parsed_params = {k: v[0] for k, v in parse_qs(query_string).items()}
        logger.info(f"Parsed params from To: {parsed_params}")

    # Source 3: JSON body 'call' object (SignalWire Call Fabric format)
    if 'call' in json_data:
        call_data = json_data.get('call', {})
        logger.info(f"Call data from JSON: {call_data}")
        # Check for user_variables or params in call data
        if 'user_variables' in call_data:
            parsed_params.update(call_data['user_variables'])
        if 'params' in call_data:
            parsed_params.update(call_data['params'])
        # Check for 'to' or 'destination' in call data (might have query params)
        json_to = call_data.get('to', '') or call_data.get('destination', '')
        if '?' in json_to:
            query_string = json_to.split('?', 1)[1]
            parsed_params.update({k: v[0] for k, v in parse_qs(query_string).items()})
            logger.info(f"Parsed params from JSON to field: {parsed_params}")

    # Also check top-level JSON for 'to', 'To', 'destination'
    json_to_toplevel = json_data.get('to', '') or json_data.get('To', '') or json_data.get('destination', '') or json_data.get('Destination', '')
    if json_to_toplevel and '?' in json_to_toplevel:
        query_string = json_to_toplevel.split('?', 1)[1]
        parsed_params.update({k: v[0] for k, v in parse_qs(query_string).items()})
        logger.info(f"Parsed params from top-level to field: {parsed_params}")

    # Source 4: JSON body 'vars' or 'variables' (another common format)
    if 'vars' in json_data:
        parsed_params.update(json_data['vars'])
    if 'variables' in json_data:
        parsed_params.update(json_data['variables'])

    # Source 5: Direct JSON body params
    if 'agent_id' in json_data:
        parsed_params['agent_id'] = json_data['agent_id']
    if 'conf' in json_data:
        parsed_params['conf'] = json_data['conf']

    # Source 6: SignalWire call_params format
    if 'call_params' in json_data:
        parsed_params.update(json_data['call_params'])

    # Source 7: Check headers for SignalWire specific info
    sw_user_vars = request.headers.get('X-SignalWire-User-Variables', '')
    if sw_user_vars:
        try:
            parsed_params.update(json.loads(sw_user_vars))
        except:
            pass

    # Source 8: Form data direct fields (for url-encoded forms)
    if request.form.get('agent_id'):
        parsed_params['agent_id'] = request.form.get('agent_id')
    if request.form.get('conf'):
        parsed_params['conf'] = request.form.get('conf')
    if request.form.get('conference_name'):
        parsed_params['conf'] = request.form.get('conference_name')

    # Source 9: Check for nested structures common in SignalWire webhooks
    for key in ['swml_vars', 'swml_params', 'dial_params', 'destination_params']:
        if key in json_data and isinstance(json_data[key], dict):
            parsed_params.update(json_data[key])

    logger.info(f"All parsed params: {parsed_params}")

    # Source 10: Redis lookup by join_token (most reliable method)
    # The frontend calls /api/conferences/prepare-join first, which stores params in Redis
    join_token = request.args.get('token') or parsed_params.get('token') or request.form.get('token')
    if join_token:
        logger.info(f"Looking up join_token in Redis: {join_token}")
        redis_key = f"conference_join:{join_token}"
        redis_data = redis_client.get(redis_key)
        if redis_data:
            try:
                token_params = json.loads(redis_data)
                logger.info(f"Found params from Redis: {token_params}")
                parsed_params.update(token_params)
                # Delete the one-time token
                redis_client.delete(redis_key)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse Redis data for token {join_token}")

    # Get conference name - if provided, use per-interaction mode
    conference_name = request.args.get('conf') or parsed_params.get('conf')

    # Get agent_id from multiple sources
    agent_id = request.args.get('agent_id') or parsed_params.get('agent_id') or request.form.get('agent_id')

    if not agent_id:
        logger.error("No agent_id provided - all sources checked")
        error_swml = {
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"url": "say:Agent ID required. Please try again."}},
                    "hangup"
                ]
            }
        }
        response = make_response(json.dumps(error_swml))
        response.headers['Content-Type'] = 'application/json'
        return response

    try:
        agent_id = int(agent_id)
    except ValueError:
        logger.error(f"Invalid agent_id: {agent_id}")
        return jsonify({
            "version": "1.0.0",
            "sections": {"main": ["hangup"]}
        })

    base_url = get_base_url()

    # Mode 1: Per-interaction conference (NEW)
    if conference_name:
        logger.info(f"Per-interaction mode: Agent {agent_id} joining conference {conference_name}")
        status_callback = f"{base_url}/api/conferences/{conference_name}/status"

        swml = {
            "version": "1.0.0",
            "sections": {
                "main": [
                    {
                        "join_conference": {
                            "name": conference_name,
                            "end_on_exit": True,
                            "beep": "onEnter",
                            "status_callback": status_callback,
                            "status_callback_event": "start end join leave"
                        }
                    }
                ]
            }
        }
        logger.info(f"Returning SWML: {json.dumps(swml)}")
        return jsonify(swml)

    # Mode 2: Per-agent conference (LEGACY - for backward compatibility)
    logger.info(f"Per-agent mode: Agent {agent_id} joining personal conference")
    conference = Conference.get_or_create_agent_conference(agent_id)
    db.session.commit()

    status_callback = f"{base_url}/api/conferences/{conference.conference_name}/status"

    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "join_conference": {
                        "name": conference.conference_name,
                        "end_on_exit": True,
                        "beep": "onEnter",
                        "status_callback": status_callback,
                        "status_callback_event": "start end join leave"
                    }
                }
            ]
        }
    }

    logger.info(f"Agent {agent_id} joining conference {conference.conference_name}")
    logger.info(f"Returning SWML: {json.dumps(swml)}")

    return jsonify(swml)


@conferences_bp.route('/join-conference', methods=['POST', 'GET'])
def join_conference_webhook():
    """SWML webhook endpoint for joining a specific conference.

    This is the NEW per-interaction model. When a customer is routed to an agent,
    an interaction conference is created, and the agent is DIALED into it.

    Query params:
        conf: The conference name to join (e.g., interaction-abc123)
        agent_id: Optional - the agent being connected (for tracking)

    Can use the same CXML/SWML Script resource as agent-conference, just pass conf param:
        /public/agent-conference?conf=interaction-abc123&agent_id=4
    """
    logger.info(f"Join conference webhook called")
    logger.info(f"Request args: {dict(request.args)}")
    logger.info(f"Request form: {dict(request.form)}")

    # Get conference name - try multiple sources
    conference_name = request.args.get('conf') or request.args.get('conference')

    # If not in query params, parse from the 'To' parameter
    if not conference_name:
        to_param = request.form.get('To', '') or request.form.get('Called', '')
        logger.info(f"Parsing conf from To parameter: {to_param}")

        if '?' in to_param:
            query_string = to_param.split('?', 1)[1]
            parsed_qs = parse_qs(query_string)
            conference_name = parsed_qs.get('conf', parsed_qs.get('conference', [None]))[0]
            logger.info(f"Extracted conf from To: {conference_name}")

    if not conference_name:
        logger.error("No conference name provided")
        # Return SWML that hangs up
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"url": "say:No conference specified"}},
                    "hangup"
                ]
            }
        })

    # Get optional tracking params
    agent_id = request.args.get('agent_id')
    if not agent_id:
        to_param = request.form.get('To', '') or request.form.get('Called', '')
        if '?' in to_param:
            query_string = to_param.split('?', 1)[1]
            parsed_qs = parse_qs(query_string)
            agent_id = parsed_qs.get('agent_id', [None])[0]

    base_url = get_base_url()
    status_callback = f"{base_url}/api/conferences/{conference_name}/status"

    # Return SWML with join_conference
    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "join_conference": {
                        "name": conference_name,
                        "end_on_exit": True,  # Conference ends when agent leaves
                        "beep": "onEnter",
                        "status_callback": status_callback,
                        "status_callback_event": "start end join leave"
                    }
                }
            ]
        }
    }

    logger.info(f"Agent {agent_id} joining interaction conference {conference_name}")
    logger.info(f"Returning SWML: {json.dumps(swml)}")

    return jsonify(swml)


@conferences_bp.route('/agent-join-swml', methods=['POST', 'GET'])
def agent_join_swml():
    """SWML endpoint for server-initiated calls to agents.

    When the backend calls an agent (via REST API), this endpoint provides
    the SWML that joins them to the interaction conference when they answer.

    Query params:
        conf: The conference name to join (e.g., interaction-abc123)
        agent_id: The agent being connected

    This is the SERVER-INITIATED pattern - no SignalWire Dashboard resource needed.
    Backend calls agent -> agent answers -> this SWML runs -> agent joins conference.
    """
    logger.info(f"Agent join SWML endpoint called")
    logger.info(f"Request args: {dict(request.args)}")
    logger.info(f"Request form: {dict(request.form)}")

    # Get conference name from query params
    conference_name = request.args.get('conf') or request.args.get('conference')
    agent_id = request.args.get('agent_id')

    if not conference_name:
        logger.error("No conference name provided to agent-join-swml")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"url": "say:Conference not found. Please try again."}},
                    "hangup"
                ]
            }
        })

    logger.info(f"Agent {agent_id} answering call, will join conference {conference_name}")

    base_url = get_base_url()
    status_callback = f"{base_url}/api/conferences/{conference_name}/status"

    # Return SWML that joins agent to the conference
    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "join_conference": {
                        "name": conference_name,
                        "end_on_exit": True,  # Conference ends when agent hangs up
                        "beep": "onEnter",
                        "status_callback": status_callback,
                        "status_callback_event": "start end join leave"
                    }
                }
            ]
        }
    }

    logger.info(f"Returning SWML for agent {agent_id} to join {conference_name}")
    logger.info(f"SWML: {json.dumps(swml)}")

    return jsonify(swml)


@conferences_bp.route('/<conference_name>/agent-call-state', methods=['POST'])
def agent_call_state_webhook(conference_name):
    """Handle call state events for server-initiated calls to agents.

    Called by SignalWire when the agent's call state changes (ringing, answered, ended).

    IMPORTANT: When the agent answers, we use the REST API to join them to the conference.
    This is necessary because Call Fabric subscribers don't support "answer URLs" like phone calls.
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info(f"Agent call state webhook for conference {conference_name}")
    logger.info(f"Data: {json.dumps(data, indent=2) if isinstance(data, dict) else data}")

    # Handle nested params structure from SignalWire
    params = data.get('params', data)
    call_sid = params.get('call_id') or data.get('CallSid')
    call_state = params.get('call_state') or data.get('CallStatus')

    logger.info(f"Agent call {call_sid} state: {call_state}")

    if call_state == 'answered':
        # Agent answered! Now join them to the conference via REST API
        logger.info(f"Agent answered - joining them to conference {conference_name}")
        try:
            from app.services.signalwire_api import SignalWireAPI
            sw_api = SignalWireAPI()

            # Use the conference join command to add the agent to the conference
            result = sw_api.add_participant_to_conference(conference_name, call_sid)
            logger.info(f"Agent {call_sid} joined conference {conference_name}: {result}")

        except Exception as e:
            logger.error(f"Failed to join agent to conference: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

    elif call_state == 'ended':
        # Agent hung up or didn't answer - might want to re-route
        end_reason = params.get('end_reason') or data.get('SipResponseCode')
        logger.info(f"Agent call ended. Reason: {end_reason}")

    return jsonify({'status': 'ok'})


@conferences_bp.route('/customer-conference', methods=['POST', 'GET'])
def customer_conference_webhook():
    """CXML webhook endpoint for customer conference join.

    Called when a customer is routed to an agent's conference.
    """
    # Get the target conference from query params
    conference_name = request.args.get('conference')

    logger.info(f"Customer conference webhook called: conference={conference_name}")

    if not conference_name:
        logger.error("No conference name provided")
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            mimetype='application/xml'
        )

    base_url = get_base_url()
    status_callback = f"{base_url}/api/conferences/{conference_name}/status"

    # Return CXML that joins the customer to the agent's conference
    cxml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
<Dial>
  <Conference statusCallback="{status_callback}"
    statusCallbackEvent="start end join leave"
    startConferenceOnEnter="false"
    endConferenceOnExit="false">{conference_name}</Conference>
</Dial>
</Response>'''

    logger.info(f"Customer joining conference {conference_name}")

    return Response(cxml, mimetype='application/xml')


# ============================================================================
# API Endpoints (called by frontend, auth required)
# ============================================================================

@conferences_bp.route('/agent/<int:agent_id>/resource-address', methods=['GET'])
@require_auth
def get_agent_conference_resource(agent_id):
    """Get the resource address for an agent's conference.

    Frontend calls this to get the address to dial when going available.
    Returns the resource address configured in AGENT_CONFERENCE_RESOURCE env var.

    Also pre-creates the conference record in the database so dial-out works
    even if SignalWire hasn't called our webhook yet.
    """
    # Get the resource address from environment
    # This should be set to the address assigned in SignalWire Dashboard
    # e.g., /public/agent-conference
    resource_address = os.getenv('AGENT_CONFERENCE_RESOURCE', '/public/agent-conference-swml')

    # Pre-create the conference record so dial-out works immediately
    # This ensures the DB record exists even if SignalWire webhook is delayed
    conference = Conference.get_or_create_agent_conference(agent_id)
    db.session.commit()

    conference_name = conference.conference_name

    # The full dial address includes the agent_id as a query param
    dial_address = f"{resource_address}?agent_id={agent_id}"

    return jsonify({
        'dial_address': dial_address,
        'conference_name': conference_name,
        'resource_address': resource_address,
        'conference_id': conference.id
    })


@conferences_bp.route('/ai/<ai_agent_name>/join', methods=['POST'])
def ai_join_conference(ai_agent_name):
    """Return SWML for joining an AI agent's conference.

    Called when a customer call needs to join an AI agent's conference.
    """
    logger.info(f"Request to join AI conference: {ai_agent_name}")

    # Get or create the AI conference
    conference = Conference.get_or_create_ai_conference(ai_agent_name)
    db.session.commit()

    base_url = get_base_url()

    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "conference": {
                        "name": conference.conference_name,
                        "status_url": f"{base_url}/api/conferences/{conference.conference_name}/status",
                        "status_events": ["join", "leave"],
                        "join_options": {
                            "start_on_enter": True
                        }
                    }
                }
            ]
        }
    }

    return jsonify(swml_response)


@conferences_bp.route('/<conference_name>/status', methods=['POST'])
def conference_status_callback(conference_name):
    """Handle SignalWire conference status callbacks.

    Events: join, leave, speak-start, speak-stop
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info(f"Conference status callback for {conference_name}: {json.dumps(data, indent=2)}")

    event_type = data.get('event_type', data.get('StatusCallbackEvent'))
    participant_call_sid = data.get('call_id', data.get('CallSid'))

    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        logger.warning(f"Conference not found: {conference_name}")
        return jsonify({'status': 'conference_not_found'}), 200

    from app import socketio

    if event_type in ['join', 'participant-join']:
        # Participant joined the conference
        logger.info(f"Participant {participant_call_sid} joined conference {conference_name}")

        # Check if this participant already exists
        existing = ConferenceParticipant.get_active_by_call_sid(participant_call_sid)
        if not existing:
            # Determine participant type
            participant_type = 'customer'  # Default
            participant_id = participant_call_sid

            # Check if this is an agent (their conference)
            if conference.conference_type == 'agent' and conference.owner_user_id:
                # First participant in agent conference is usually the agent themselves
                agent_participant = conference.get_agent_participant()
                if not agent_participant:
                    participant_type = 'agent'
                    participant_id = str(conference.owner_user_id)

            # Check if call record exists to get proper call_id
            call = Call.find_by_sid(participant_call_sid)
            call_id = call.id if call else None

            participant = ConferenceParticipant(
                conference_id=conference.id,
                call_id=call_id,
                participant_type=participant_type,
                participant_id=participant_id,
                call_sid=participant_call_sid,
                status='active',
                joined_at=datetime.utcnow()
            )
            db.session.add(participant)
            db.session.commit()

            # Emit socket event
            socketio.emit('conference_participant_joined', {
                'conference_name': conference_name,
                'conference_id': conference.id,
                'participant': participant.to_dict(),
                'participant_count': conference.get_active_participant_count()
            }, room=f'conference:{conference_name}')

            # Update call leg status from 'connecting' to 'active' if this is a customer joining
            if participant_type == 'customer' and call:
                # Find the call leg that's in 'connecting' status for this conference
                connecting_leg = CallLeg.query.filter_by(
                    call_id=call.id,
                    conference_name=conference_name,
                    status='connecting'
                ).first()

                if connecting_leg:
                    connecting_leg.status = 'active'
                    logger.info(f"Updated call leg {connecting_leg.id} status to 'active'")

                # Update call status to 'active' (human handling)
                # Include 'ringing' for outbound calls initiated via dial-out
                if call.status in ['connecting', 'queued', 'ringing']:
                    call.status = 'active'
                    call.handler_type = 'human'
                    logger.info(f"Updated call {call.id} status to 'active'")

                db.session.commit()

                # Emit call_update so frontend gets real-time update
                emit_call_update(call)

            # Also emit to agent room if customer joined
            # Room name must match what authenticate handler uses: str(user_id)
            if participant_type == 'customer' and conference.owner_user_id:
                socketio.emit('customer_routed_to_conference', {
                    'conference_name': conference_name,
                    'customer_call_sid': participant_call_sid,
                    'call_id': call_id,
                    'conference_id': conference.id,
                    'customer_info': {
                        'phone': call.from_number if call else participant_call_sid
                    }
                }, room=str(conference.owner_user_id))

    elif event_type in ['leave', 'participant-leave']:
        # Participant left the conference
        logger.info(f"Participant {participant_call_sid} left conference {conference_name}")

        participant = ConferenceParticipant.get_active_by_call_sid(participant_call_sid)
        if participant:
            participant.leave()

            # If customer left, end the call leg and update call status
            if participant.participant_type == 'customer':
                call = Call.find_by_sid(participant_call_sid)
                if call:
                    # End the active call leg for this conference
                    active_leg = CallLeg.query.filter_by(
                        call_id=call.id,
                        conference_name=conference_name,
                        status='active'
                    ).first()

                    if active_leg:
                        active_leg.end_leg(reason='customer_left')
                        logger.info(f"Ended call leg {active_leg.id} - customer left conference")

                    # Update call status to completed
                    if call.status not in ['completed', 'ended']:
                        call.status = 'completed'
                        call.ended_at = datetime.utcnow()
                        logger.info(f"Updated call {call.id} status to 'completed'")

                    db.session.commit()

                    # Emit call_update so frontend removes from active calls
                    emit_call_update(call)

            db.session.commit()

            # Emit socket event
            socketio.emit('conference_participant_left', {
                'conference_name': conference_name,
                'conference_id': conference.id,
                'participant': participant.to_dict(),
                'participant_count': conference.get_active_participant_count()
            }, room=f'conference:{conference_name}')

            # If customer left, notify the agent
            # Room name must match what authenticate handler uses: str(user_id)
            if participant.participant_type == 'customer' and conference.owner_user_id:
                socketio.emit('customer_left_conference', {
                    'conference_name': conference_name,
                    'customer_call_sid': participant_call_sid,
                    'participant_id': participant.participant_id
                }, room=str(conference.owner_user_id))

    elif event_type in ['speak-start', 'speak-stop']:
        # Speaking status change - useful for UI indicators
        is_speaking = event_type == 'speak-start'
        socketio.emit('conference_participant_speaking', {
            'conference_name': conference_name,
            'participant_call_sid': participant_call_sid,
            'is_speaking': is_speaking
        }, room=f'conference:{conference_name}')

    return jsonify({'status': 'ok'})


@conferences_bp.route('/<conference_name>/participants', methods=['GET'])
@require_auth
def get_conference_participants(conference_name):
    """Get list of participants in a conference."""
    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        return jsonify({'error': 'Conference not found'}), 404

    participants = conference.participants.filter_by(status='active').all()

    return jsonify({
        'conference': conference.to_dict(),
        'participants': [p.to_dict() for p in participants]
    })


@conferences_bp.route('/<conference_name>/move-participant', methods=['POST'])
@require_auth
def move_participant(conference_name):
    """Move a participant from one conference to another.

    This is used for:
    - Takeover: Move customer from AI conference to agent conference
    - Transfer: Move customer from one agent to another
    """
    data = request.get_json()
    participant_call_sid = data.get('participant_call_sid')
    target_conference_name = data.get('target_conference')

    if not participant_call_sid or not target_conference_name:
        return jsonify({'error': 'participant_call_sid and target_conference required'}), 400

    # Find source conference
    source_conference = Conference.get_active_by_name(conference_name)
    if not source_conference:
        return jsonify({'error': 'Source conference not found'}), 404

    # Find target conference
    target_conference = Conference.get_active_by_name(target_conference_name)
    if not target_conference:
        return jsonify({'error': 'Target conference not found'}), 404

    # Find the participant
    participant = ConferenceParticipant.get_active_by_call_sid(participant_call_sid)
    if not participant:
        return jsonify({'error': 'Participant not found in source conference'}), 404

    # Use SignalWire API to move the participant
    # This requires calling the SignalWire REST API
    from app.services.signalwire_api import SignalWireAPI
    sw_api = SignalWireAPI()

    try:
        # Remove from source conference
        sw_api.remove_participant_from_conference(conference_name, participant_call_sid)

        # Add to target conference
        result = sw_api.add_participant_to_conference(
            target_conference_name,
            participant_call_sid
        )

        # Update database records
        participant.leave()

        # Create new participant record in target conference
        new_participant = ConferenceParticipant(
            conference_id=target_conference.id,
            call_id=participant.call_id,
            participant_type=participant.participant_type,
            participant_id=participant.participant_id,
            call_sid=participant_call_sid,
            status='active',
            joined_at=datetime.utcnow()
        )
        db.session.add(new_participant)
        db.session.commit()

        # Emit events
        from app import socketio
        socketio.emit('participant_moved', {
            'from_conference': conference_name,
            'to_conference': target_conference_name,
            'participant_call_sid': participant_call_sid
        })

        return jsonify({
            'success': True,
            'from_conference': conference_name,
            'to_conference': target_conference_name,
            'participant': new_participant.to_dict()
        })

    except Exception as e:
        logger.error(f"Failed to move participant: {str(e)}")
        return jsonify({'error': str(e)}), 500


@conferences_bp.route('/<conference_name>/end', methods=['POST'])
@require_auth
def end_conference(conference_name):
    """End a conference and disconnect all participants."""
    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        return jsonify({'error': 'Conference not found'}), 404

    try:
        # Use SignalWire API to end the conference
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()
        sw_api.end_conference(conference_name)

        # Update database
        conference.end_conference()
        db.session.commit()

        # Emit event
        from app import socketio
        socketio.emit('conference_ended', {
            'conference_name': conference_name,
            'conference_id': conference.id
        })

        return jsonify({'success': True, 'conference_name': conference_name})

    except Exception as e:
        logger.error(f"Failed to end conference: {str(e)}")
        return jsonify({'error': str(e)}), 500


@conferences_bp.route('/<conference_name>/mute/<participant_call_sid>', methods=['POST'])
@require_auth
def mute_participant(conference_name, participant_call_sid):
    """Mute or unmute a participant."""
    data = request.get_json() or {}
    muted = data.get('muted', True)

    participant = ConferenceParticipant.get_active_by_call_sid(participant_call_sid)
    if not participant:
        return jsonify({'error': 'Participant not found'}), 404

    try:
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()
        sw_api.mute_participant(conference_name, participant_call_sid, muted)

        participant.mute(muted)
        db.session.commit()

        return jsonify({
            'success': True,
            'participant_call_sid': participant_call_sid,
            'muted': muted
        })

    except Exception as e:
        logger.error(f"Failed to mute participant: {str(e)}")
        return jsonify({'error': str(e)}), 500


@conferences_bp.route('/active', methods=['GET'])
@require_auth
def get_active_conferences():
    """Get all active conferences."""
    conferences = db.session.query(Conference).filter_by(status='active').all()

    return jsonify({
        'conferences': [c.to_dict(include_participants=True) for c in conferences]
    })


# ============================================================================
# Dial-Out Endpoints (for outbound calls that join conference)
# ============================================================================

@conferences_bp.route('/<conference_name>/dial-out', methods=['POST'])
@require_auth
def dial_out_to_conference(conference_name):
    """Initiate an outbound call and connect them to the agent's conference.

    This is used when an agent (already in their conference) wants to call someone.
    The called party, when they answer, is connected into the agent's conference.
    """
    data = request.get_json()
    phone_number = data.get('phone_number')
    contact_id = data.get('contact_id')
    context = data.get('context', {})

    if not phone_number:
        return jsonify({'error': 'phone_number is required'}), 400

    # Validate conference exists
    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        return jsonify({'error': 'Conference not found'}), 404

    # Verify the requesting user owns this conference
    # Use request.current_user set by @require_auth decorator
    current_user_id = request.current_user.id
    if conference.owner_user_id != current_user_id:
        return jsonify({'error': 'You do not own this conference'}), 403

    try:
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()

        base_url = get_base_url()
        # When the call is answered, this webhook returns SWML to join the conference
        answer_url = f"{base_url}/api/conferences/{conference_name}/outbound-answer"
        # Call state events (ringing, answered, ended) go to the call-state endpoint
        call_state_url = f"{base_url}/api/conferences/{conference_name}/call-state"

        logger.info(f"Dial-out: {phone_number} -> conference {conference_name}")
        logger.info(f"Answer URL (SWML): {answer_url}")
        logger.info(f"Call State URL: {call_state_url}")

        # Create the outbound call
        result = sw_api.create_call(
            to=phone_number,
            swml_url=answer_url,
            status_callback=call_state_url
        )

        call_sid = result.sid if hasattr(result, 'sid') else result.get('call_id')

        # Create a Call record for tracking
        call = Call(
            signalwire_call_sid=call_sid,
            user_id=current_user_id,
            contact_id=contact_id,
            from_number=sw_api.from_number,
            destination=phone_number,
            destination_type='phone',
            direction='outbound',
            handler_type='human',
            status='ringing',
            created_at=datetime.utcnow()
        )
        db.session.add(call)
        db.session.commit()

        # Emit call update so frontend knows about the new call
        emit_call_update(call)

        return jsonify({
            'success': True,
            'call_sid': call_sid,
            'call_id': call.id,
            'conference_name': conference_name,
            'phone_number': phone_number
        })

    except Exception as e:
        logger.error(f"Failed to dial out: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@conferences_bp.route('/<conference_name>/outbound-answer', methods=['POST', 'GET'])
def outbound_answer_webhook(conference_name):
    """SWML webhook for when an outbound call is answered.

    Returns SWML with join_conference that adds the answered party to the agent's conference.
    No auth required - this is called by SignalWire.
    """
    logger.info(f"Outbound call answered, joining conference: {conference_name}")
    logger.info(f"Request args: {dict(request.args)}")
    logger.info(f"Request form: {dict(request.form)}")

    # Get call info from SignalWire
    call_sid = request.form.get('CallSid') or request.form.get('call_id')
    from_number = request.form.get('From')
    to_number = request.form.get('To')

    logger.info(f"Call SID: {call_sid}, From: {from_number}, To: {to_number}")

    base_url = get_base_url()
    status_callback = f"{base_url}/api/conferences/{conference_name}/status"

    # Return SWML that joins the called party to the agent's conference
    # Using join_conference method as per SignalWire docs
    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "join_conference": {
                        "name": conference_name,
                        "start_on_enter": False,  # Agent should already be there
                        "end_on_exit": False,     # Don't end conf when customer leaves
                        "beep": "onEnter",        # Beep when customer joins
                        "status_callback": status_callback,
                        "status_callback_event": "join leave"
                    }
                }
            ]
        }
    }

    logger.info(f"Returning SWML to join conference {conference_name}")
    logger.info(f"SWML: {json.dumps(swml, indent=2)}")

    return jsonify(swml)


@conferences_bp.route('/<conference_name>/call-state', methods=['POST'])
def call_state_webhook(conference_name):
    """Handle call state events for outbound calls.

    Called by SignalWire when call state changes (created, ringing, answered, ended).
    Updates the Call record and emits socket events to update the UI.
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info(f"Call state webhook for conference {conference_name}")
    logger.info(f"Data: {json.dumps(data, indent=2) if isinstance(data, dict) else data}")

    call_sid = data.get('call_id') or data.get('CallSid')
    call_state = data.get('call_state') or data.get('CallStatus')

    if not call_sid:
        logger.warning("No call_id in call state webhook")
        return jsonify({'status': 'no_call_id'}), 200

    # Find the call record
    call = Call.find_by_sid(call_sid)
    if not call:
        logger.warning(f"Call not found for SID: {call_sid}")
        return jsonify({'status': 'call_not_found'}), 200

    # Map SignalWire call states to our statuses
    state_mapping = {
        'created': 'ringing',
        'ringing': 'ringing',
        'answered': 'active',
        'ended': 'ended',
        'failed': 'failed',
        'busy': 'failed',
        'no-answer': 'failed'
    }

    new_status = state_mapping.get(call_state, call_state)
    old_status = call.status

    if new_status and new_status != old_status:
        call.status = new_status
        logger.info(f"Call {call.id} status: {old_status} -> {new_status}")

        if new_status == 'active':
            call.answered_at = datetime.utcnow()
        elif new_status in ['ended', 'failed']:
            call.ended_at = datetime.utcnow()

        db.session.commit()

        # Emit update to frontend
        emit_call_update(call)

    return jsonify({'status': 'ok'})
