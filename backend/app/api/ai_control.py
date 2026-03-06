"""
AI Call Control and Intervention
Allows supervisors to monitor and control active AI agent calls in real-time
"""

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import requests
import os
import logging
import json
import base64
from datetime import datetime
from base64 import b64encode

logger = logging.getLogger(__name__)

ai_control_bp = Blueprint('ai_control', __name__)

# Available AI agents - must match routes in ai-agents/main_agent.py
AI_AGENTS = [
    {
        'id': 'outbound-sales',
        'name': 'Outbound Sales Agent',
        'route': '/outbound-sales',
        'type': 'outbound',
        'description': 'Proactive sales outreach with customer context'
    },
    {
        'id': 'outbound-support',
        'name': 'Outbound Support Agent',
        'route': '/outbound-support',
        'type': 'outbound',
        'description': 'Proactive support follow-up with customer context'
    },
    {
        'id': 'sales-ai',
        'name': 'Sales AI Specialist',
        'route': '/sales-ai',
        'type': 'inbound',
        'description': 'General sales help (designed for inbound transfers)'
    },
    {
        'id': 'support-ai',
        'name': 'Support AI Specialist',
        'route': '/support-ai',
        'type': 'inbound',
        'description': 'General support help (designed for inbound transfers)'
    },
    {
        'id': 'receptionist',
        'name': 'Receptionist / Triage',
        'route': '/receptionist',
        'type': 'inbound',
        'description': 'Call triage and routing (designed for inbound)'
    },
]

AGENT_ROUTE_MAP = {a['id']: a['route'] for a in AI_AGENTS}

# SignalWire configuration
SIGNALWIRE_SPACE = os.getenv('SIGNALWIRE_SPACE')
SIGNALWIRE_PROJECT_KEY = os.getenv('SIGNALWIRE_PROJECT_ID')
SIGNALWIRE_TOKEN = os.getenv('SIGNALWIRE_API_TOKEN')

def get_signalwire_auth_headers():
    """Get authentication headers for SignalWire API."""
    credentials = f"{SIGNALWIRE_PROJECT_KEY}:{SIGNALWIRE_TOKEN}"
    auth = b64encode(credentials.encode()).decode('ascii')
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {auth}'
    }


@ai_control_bp.route('/active-sessions', methods=['GET'])
@jwt_required()
def get_active_ai_sessions():
    """
    Get all currently active AI agent calls.
    Returns list of calls with transcription and metadata.
    """
    try:
        # Query SignalWire for active calls
        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls"

        response = requests.get(
            url,
            headers=get_signalwire_auth_headers(),
            params={
                'status': 'in-progress'
            }
        )

        if response.status_code != 200:
            logger.error(f"Failed to fetch active calls: {response.text}")
            return jsonify({'error': 'Failed to fetch active calls'}), 500

        calls_data = response.json()

        # Filter for AI agent calls and enrich with additional data
        ai_calls = []
        for call in calls_data.get('data', []):
            # Check if this is an AI agent call (you might have specific markers)
            to_address = call.get('to', '')

            # Only include AI agent calls (e.g., those going to /ai/ or /public/ AI endpoints)
            if any(keyword in to_address for keyword in ['/ai-', '/public/ai', 'agent', 'receptionist']):
                # Get transcription for this call
                transcription = get_call_transcription(call['id'])

                # Get current sentiment/status
                call_details = get_call_details(call['id'])

                ai_calls.append({
                    'call_id': call['id'],
                    'from': call.get('from', 'Unknown'),
                    'to': to_address,
                    'ai_agent': extract_agent_name(to_address),
                    'duration': call.get('duration', 0),
                    'start_time': call.get('start_time'),
                    'transcription': transcription,
                    'current_sentiment': call_details.get('sentiment', 0),
                    'can_inject': True,
                    'metadata': call.get('call_state', {})
                })

        return jsonify({
            'success': True,
            'active_ai_calls': ai_calls,
            'count': len(ai_calls)
        })

    except Exception as e:
        logger.error(f"Error fetching active AI sessions: {e}")
        return jsonify({'error': str(e)}), 500


@ai_control_bp.route('/inject-message', methods=['POST'])
@jwt_required()
def inject_system_message():
    """
    Inject a system message into an active AI call to redirect its behavior.

    Request body:
    {
        "call_id": "call-uuid",
        "message": "Offer the customer a 20% discount",
        "role": "system"  // optional, defaults to "system"
    }
    """
    try:
        logger.info("🎯 AI INJECT-MESSAGE ENDPOINT HIT!")
        supervisor_id = get_jwt_identity()
        logger.info(f"🎯 Supervisor ID: {supervisor_id}")
        data = request.get_json()
        logger.info(f"🎯 Request data: {data}")

        call_id = data.get('call_id')
        message_text = data.get('message')
        role = data.get('role', 'system')  # system, user, or assistant

        logger.info(f"🎯 Parsed - call_id: {call_id}, message: {message_text}, role: {role}")

        if not call_id or not message_text:
            logger.error(f"🎯 Missing required fields - call_id: {call_id}, message: {message_text}")
            return jsonify({'error': 'call_id and message are required'}), 400

        # Log the intervention
        logger.info(f"Supervisor {supervisor_id} injecting message into call {call_id}: {message_text}")

        # Use SignalWire's calling.ai_message command
        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls"

        payload = {
            "id": call_id,
            "command": "calling.ai_message",
            "params": {
                "role": role,
                "message_text": message_text
            }
        }

        response = requests.post(
            url,
            json=payload,
            headers=get_signalwire_auth_headers()
        )

        if response.status_code not in [200, 201, 204]:
            logger.error(f"Failed to inject message: {response.text}")
            return jsonify({
                'error': 'Failed to inject message',
                'details': response.text
            }), 500

        # Store injection in database for audit trail
        from app.services.redis_service import get_redis_client
        redis_client = get_redis_client()
        if redis_client:
            injection_record = {
                'call_id': call_id,
                'supervisor_id': supervisor_id,
                'message': message_text,
                'role': role,
                'timestamp': datetime.utcnow().isoformat(),
                'status': 'injected'
            }
            redis_client.lpush(
                f'ai_injection:{call_id}',
                str(injection_record)
            )

        return jsonify({
            'success': True,
            'message': 'System message injected successfully',
            'call_id': call_id,
            'injection_time': datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.error(f"Error injecting system message: {e}")
        return jsonify({'error': str(e)}), 500


@ai_control_bp.route('/injection-history/<call_id>', methods=['GET'])
@jwt_required()
def get_injection_history(call_id):
    """Get history of all system message injections for a specific call."""
    try:
        from app.services.redis_service import get_redis_client
        redis_client = get_redis_client()

        if not redis_client:
            return jsonify({'history': []})

        # Get injection history from Redis
        history = redis_client.lrange(f'ai_injection:{call_id}', 0, -1)

        # Parse and return
        injections = [eval(h) for h in history]  # Safe here since we control the data

        return jsonify({
            'call_id': call_id,
            'history': injections,
            'count': len(injections)
        })

    except Exception as e:
        logger.error(f"Error fetching injection history: {e}")
        return jsonify({'error': str(e)}), 500


@ai_control_bp.route('/transcription/<call_id>', methods=['GET'])
def get_call_transcription(call_id):
    """
    Get real-time transcription for an active call.
    This streams transcription updates.
    """
    try:
        # Query SignalWire for call transcription
        # This would use SignalWire's transcription API
        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls/{call_id}/transcription"

        response = requests.get(
            url,
            headers=get_signalwire_auth_headers()
        )

        if response.status_code != 200:
            # Transcription might not be available yet
            return []

        transcription_data = response.json()

        # Format transcription for UI
        messages = []
        for entry in transcription_data.get('transcripts', []):
            messages.append({
                'timestamp': entry.get('timestamp'),
                'speaker': entry.get('speaker', 'unknown'),
                'text': entry.get('text'),
                'confidence': entry.get('confidence', 1.0),
                'sentiment': entry.get('sentiment')
            })

        return messages

    except Exception as e:
        logger.error(f"Error fetching transcription: {e}")
        return []


def get_call_details(call_id):
    """Get detailed call state including sentiment and metadata."""
    try:
        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls/{call_id}"

        response = requests.get(
            url,
            headers=get_signalwire_auth_headers()
        )

        if response.status_code != 200:
            return {}

        call_data = response.json()

        return {
            'sentiment': call_data.get('sentiment', 0),
            'state': call_data.get('state'),
            'duration': call_data.get('duration', 0),
            'metadata': call_data.get('metadata', {})
        }

    except Exception as e:
        logger.error(f"Error fetching call details: {e}")
        return {}


def extract_agent_name(address):
    """Extract friendly agent name from SignalWire address."""
    # /public/ai-sales -> "Sales AI"
    # /ai-support -> "Support AI"
    if '/ai-' in address:
        name = address.split('/ai-')[-1]
        return f"{name.replace('-', ' ').title()} AI"
    elif '/public/' in address:
        name = address.split('/public/')[-1]
        return f"{name.replace('-', ' ').title()}"
    else:
        return address


@ai_control_bp.route('/templates', methods=['GET'])
@jwt_required()
def get_message_templates():
    """
    Get predefined system message templates for quick injection.
    These are common interventions supervisors might need.
    """
    templates = [
        {
            'id': 'offer_discount',
            'label': 'Offer Discount',
            'message': 'The customer qualifies for a special 20% discount today. Mention this and help them complete their purchase.',
            'category': 'sales'
        },
        {
            'id': 'transfer_human',
            'label': 'Transfer to Human',
            'message': 'This customer needs specialized help. Let them know you\'re transferring to a senior specialist and initiate the transfer.',
            'category': 'escalation'
        },
        {
            'id': 'schedule_callback',
            'label': 'Schedule Callback',
            'message': 'Offer to schedule a callback at a convenient time for the customer instead of keeping them on hold.',
            'category': 'service'
        },
        {
            'id': 'apologize_empathize',
            'label': 'Apologize & Empathize',
            'message': 'Acknowledge the customer\'s frustration with genuine empathy. Apologize for any inconvenience and focus on resolving their issue.',
            'category': 'service'
        },
        {
            'id': 'upsell_premium',
            'label': 'Suggest Premium',
            'message': 'Based on the customer\'s needs, our premium plan would be a better fit. Explain the additional benefits they would receive.',
            'category': 'sales'
        },
        {
            'id': 'ask_details',
            'label': 'Gather More Details',
            'message': 'Ask more specific questions about the customer\'s situation to better understand how we can help.',
            'category': 'qualification'
        },
        {
            'id': 'close_sale',
            'label': 'Close the Sale',
            'message': 'The customer seems ready to proceed. Move confidently toward completing the sale and ask for commitment.',
            'category': 'sales'
        },
        {
            'id': 'technical_handoff',
            'label': 'Technical Escalation',
            'message': 'This requires technical expertise beyond your scope. Transfer to our technical support team with full context.',
            'category': 'escalation'
        }
    ]

    return jsonify({
        'templates': templates,
        'categories': ['sales', 'service', 'escalation', 'qualification']
    })


@ai_control_bp.route('/agents', methods=['GET'])
@jwt_required()
def list_ai_agents():
    """Return the list of available AI agents."""
    return jsonify({'agents': AI_AGENTS})


@ai_control_bp.route('/outbound-swml/<int:call_id>', methods=['POST', 'GET'])
def outbound_ai_swml(call_id):
    """SWML webhook called by SignalWire when the outbound call is answered.

    Looks up the Call record to determine which AI agent to transfer to and
    what context to pass along, then returns SWML that transfers the call
    to the appropriate AI agent URL with encoded context.
    """
    from app import db
    from app.models import Call
    from app.utils.url_utils import get_base_url

    logger.info(f"Outbound AI SWML webhook called for call_id={call_id}")

    call = db.session.get(Call, call_id)
    if not call:
        logger.error(f"Call {call_id} not found for outbound SWML")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"url": "say:Sorry, an error occurred. Please try again later."}},
                    "hangup"
                ]
            }
        })

    agent_type = call.ai_agent_name or 'receptionist'
    agent_route = AGENT_ROUTE_MAP.get(agent_type, f'/{agent_type}')
    context = call.ai_context_dict

    # Build the external agent URL
    base_url = get_base_url()
    # AI agents are exposed on port 8080 behind the same proxy/ngrok,
    # but on a different path from the backend. The agent routes are at the root.
    # Use EXTERNAL_URL for the agent host since nginx routes /receptionist, /sales-ai, etc. to ai-agents.
    agent_url = f"{base_url}{agent_route}"

    # Encode context as base64 query param (same pattern as queue routing)
    if context:
        context_json = json.dumps(context)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        agent_url = f"{agent_url}?ctx={context_b64}"

    status_callback = f"{base_url}/api/webhooks/call-status"

    logger.info(f"Outbound AI SWML: transferring to {agent_url}")

    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "set": {
                        "call_state_url": status_callback,
                        "call_state_events": "created,ringing,answered,ended"
                    }
                },
                "answer",
                {
                    "transfer": {
                        "dest": agent_url
                    }
                }
            ]
        }
    }

    return jsonify(swml)


@ai_control_bp.route('/outbound-call', methods=['POST'])
@jwt_required()
def initiate_outbound_ai_call():
    """
    Initiate an outbound call handled by an AI agent.

    Request body:
    {
        "phone": "+1234567890",
        "contact_id": 123,
        "agent_type": "sales",
        "context": {
            "contact_name": "John Doe",
            "account_tier": "enterprise",
            ...
        }
    }
    """
    try:
        from app import db
        from app.models import Call
        from app.services.signalwire_api import get_signalwire_api
        from app.utils.url_utils import get_base_url

        user_id = get_jwt_identity()
        data = request.get_json()

        phone = data.get('phone')
        contact_id = data.get('contact_id')
        agent_type = data.get('agent_type', 'sales')
        context = data.get('context', {})

        if not phone:
            return jsonify({'error': 'phone is required'}), 400

        if agent_type not in AGENT_ROUTE_MAP:
            return jsonify({'error': f'Unknown agent type: {agent_type}'}), 400

        logger.info(f"User {user_id} initiating outbound AI call to {phone} with agent {agent_type}")

        # Create Call record FIRST — we need its ID for the SWML webhook URL
        context['contact_id'] = contact_id
        context['initiated_by'] = user_id
        context['call_type'] = 'outbound_ai'

        call = Call(
            user_id=user_id,
            from_number=os.getenv('SIGNALWIRE_PHONE_NUMBER', os.getenv('SIGNALWIRE_FROM_NUMBER')),
            destination=phone,
            destination_type='phone',
            status='initiated',
            direction='outbound',
            handler_type='ai',
            ai_agent_name=agent_type,
            contact_id=contact_id,
        )
        call.ai_context_dict = context
        db.session.add(call)
        db.session.commit()

        # Build the SWML webhook URL that SignalWire will fetch when the call is answered
        base_url = get_base_url()
        swml_url = f"{base_url}/api/ai/outbound-swml/{call.id}"
        status_callback = f"{base_url}/api/webhooks/call-status"

        logger.info(f"SWML URL for outbound AI call: {swml_url}")

        # Dial the customer using SignalWireAPI (correct command/params format)
        sw_api = get_signalwire_api()
        result = sw_api.create_call(phone, swml_url, status_callback=status_callback)

        call_sid = result.sid
        call.signalwire_call_sid = call_sid
        call.status = 'ai_active'
        db.session.commit()

        # Emit socket event so the dashboard sees this call immediately
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        logger.info(f"Outbound AI call initiated: {call_sid} (db id: {call.id})")

        return jsonify({
            'success': True,
            'call_sid': call_sid,
            'call_id': call.id,
            'agent_type': agent_type,
            'destination': phone,
            'status': 'ai_active'
        })

    except Exception as e:
        logger.error(f"Error initiating outbound AI call: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@ai_control_bp.route('/pause/<call_id>', methods=['POST'])
@jwt_required()
def pause_ai_agent(call_id):
    """
    Temporarily pause AI agent to allow supervisor intervention.
    """
    try:
        # This would put the AI on hold while supervisor talks to customer
        payload = {
            "id": call_id,
            "command": "calling.ai_pause",
            "params": {
                "reason": "supervisor_intervention"
            }
        }

        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls"
        response = requests.post(
            url,
            json=payload,
            headers=get_signalwire_auth_headers()
        )

        if response.status_code not in [200, 201, 204]:
            return jsonify({'error': 'Failed to pause AI'}), 500

        return jsonify({
            'success': True,
            'message': 'AI agent paused'
        })

    except Exception as e:
        logger.error(f"Error pausing AI: {e}")
        return jsonify({'error': str(e)}), 500


@ai_control_bp.route('/resume/<call_id>', methods=['POST'])
@jwt_required()
def resume_ai_agent(call_id):
    """Resume AI agent after supervisor intervention."""
    try:
        payload = {
            "id": call_id,
            "command": "calling.ai_resume"
        }

        url = f"https://{SIGNALWIRE_SPACE}/api/calling/calls"
        response = requests.post(
            url,
            json=payload,
            headers=get_signalwire_auth_headers()
        )

        if response.status_code not in [200, 201, 204]:
            return jsonify({'error': 'Failed to resume AI'}), 500

        return jsonify({
            'success': True,
            'message': 'AI agent resumed'
        })

    except Exception as e:
        logger.error(f"Error resuming AI: {e}")
        return jsonify({'error': str(e)}), 500