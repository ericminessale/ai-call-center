from flask import Blueprint, request, jsonify, Response, make_response
from app import db, redis_client
from app.models import Conference, ConferenceParticipant, Call, CallLeg, User
from app.models.user import SUPERVISORY_ROLES
from app.services.callcenter_socketio import emit_call_update
from app.utils.decorators import require_auth
from app.utils.request_logging import mask_phone, payload_keys, request_summary
from app.utils.url_utils import get_base_url, signed_webhook_url
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
# Helpers
# ============================================================================

def _maybe_start_live_translate(call, participant_call_sid):
    """Start live_translate on the customer leg if the call is flagged for it.

    Called when the customer joins the interaction conference. Reads
    `call.needs_translation` (set during queue routing) and the agent's first
    language to pick the target. Failure is logged but never raised — translation
    is best-effort, the call should still connect.
    """
    if not call or not call.needs_translation or not call.caller_language:
        return

    # Pick target language from the assigned agent's profile
    agent = User.query.get(call.assigned_agent_id) if call.assigned_agent_id else None
    if not agent:
        logger.warning(f"Call {call.id} flagged needs_translation but no assigned agent")
        return

    agent_langs = agent.languages or ['en-US']
    to_lang = agent_langs[0]
    from_lang = call.caller_language

    if from_lang == to_lang:
        # Routing flagged it but languages match — nothing to translate
        return

    try:
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()
        sw_api.start_live_translate(
            call_id=participant_call_sid,
            from_lang=from_lang,
            to_lang=to_lang,
        )
        logger.info(
            f"Started live_translate on call {call.id} (sid={participant_call_sid}): "
            f"{from_lang} <-> {to_lang}"
        )
    except Exception as e:
        logger.error(f"Failed to start live_translate for call {call.id}: {e}")


def build_whisper_text(context: dict) -> str:
    """Build a natural-language TTS whisper from AI-collected context.

    This is played to the agent BEFORE they join the conference,
    giving them a summary of what the AI gathered from the caller.
    Only includes fields that are present and non-empty.
    """
    if not context:
        return ""

    parts = []

    # Direct-inbound calls (phone number set to human_direct mode) skip the AI
    # entirely — no name / issue / priority was collected. Tell the agent so
    # they don't expect the usual pre-collected context on screen.
    if context.get('source') == 'direct_inbound':
        queue = context.get('queue')
        if queue:
            parts.append(f"Direct line for {queue}, no AI screening")
        else:
            parts.append("Direct line, no AI screening")

    # Caller identity
    name = context.get('customer_name') or context.get('caller_name')
    company = context.get('company')
    if name and company:
        parts.append(f"Call from {name} at {company}")
    elif name:
        parts.append(f"Call from {name}")
    elif company:
        parts.append(f"Call from {company}")

    # Department / queue
    dept = context.get('department') or context.get('queue')
    if dept:
        parts.append(f"for {dept}")

    # Issue / reason
    issue = context.get('issue_description') or context.get('issue') or context.get('reason')
    if issue:
        parts.append(f"regarding {issue}")

    # Priority / urgency
    priority = context.get('priority') or context.get('urgency')
    if priority:
        parts.append(f"Priority: {priority}")

    # Account number
    account = context.get('account_number')
    if account:
        parts.append(f"Account: {account}")

    # AI summary (brief, if available and nothing else covered it)
    summary = context.get('ai_summary')
    if summary and not issue:
        parts.append(summary)

    if not parts:
        return ""

    return ". ".join(parts) + "."


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

    # Optional fields for multi-agent conference modes
    join_type = data.get('type')              # 'monitor', 'backup', 'escalation', or None (normal)
    context = data.get('context')             # AI-collected context for whisper
    whisper_mode = data.get('whisper_mode')   # True for supervisor coach mode
    agent_call_sid = data.get('agent_call_sid')  # Agent's SID for coach targeting

    # Generate a unique token
    token = str(uuid.uuid4())

    # Store params in Redis with 5-minute TTL (should only take seconds to use)
    redis_key = f"conference_join:{token}"
    token_data = {
        'agent_id': agent_id,
        'conf': conference_name,
        'call_id': call_id,
    }
    # Include optional mode fields if provided
    if join_type:
        token_data['type'] = join_type
    if context:
        token_data['context'] = context
    if whisper_mode:
        token_data['whisper_mode'] = True
        if agent_call_sid:
            token_data['agent_call_sid'] = agent_call_sid

    redis_data = json.dumps(token_data)
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
    logger.info("Agent conference webhook called: %s", request_summary(request))

    # Try to get JSON body if present
    json_data = {}
    if request.is_json:
        json_data = request.get_json() or {}
        logger.debug("Agent conference JSON fields: %s", payload_keys(json_data))

    # Parse query params from multiple sources
    parsed_params = {}

    # Source 1: URL query params (request.args)
    # Source 2: Form data 'To' or 'Called' field (may contain query string)
    to_param = request.form.get('To', '') or request.form.get('Called', '') or request.form.get('to', '') or request.form.get('called', '')

    if '?' in to_param:
        query_string = to_param.split('?', 1)[1]
        parsed_params = {k: v[0] for k, v in parse_qs(query_string).items()}
        logger.debug("Parsed parameter fields from To: %s", payload_keys(parsed_params))

    # Source 3: JSON body 'call' object (SignalWire Call Fabric format)
    if 'call' in json_data:
        call_data = json_data.get('call', {})
        logger.debug("Agent conference call fields: %s", payload_keys(call_data))
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
            logger.debug(
                "Parsed parameter fields from JSON To: %s",
                payload_keys(parsed_params),
            )

    # Also check top-level JSON for 'to', 'To', 'destination'
    json_to_toplevel = json_data.get('to', '') or json_data.get('To', '') or json_data.get('destination', '') or json_data.get('Destination', '')
    if json_to_toplevel and '?' in json_to_toplevel:
        query_string = json_to_toplevel.split('?', 1)[1]
        parsed_params.update({k: v[0] for k, v in parse_qs(query_string).items()})
        logger.debug(
            "Parsed parameter fields from top-level To: %s",
            payload_keys(parsed_params),
        )

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
        except (ValueError, TypeError) as exc:
            logger.debug("X-SignalWire-User-Variables header was not valid JSON: %s", exc)

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

    logger.debug("Agent conference parsed parameter fields: %s", payload_keys(parsed_params))

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
                logger.debug(
                    "Found Redis join-token parameter fields: %s",
                    payload_keys(token_params),
                )
                parsed_params.update(token_params)
                # DO NOT delete the token here. SignalWire fetches this
                # SWML URL multiple times per dial (e.g. PSTN-leg node and
                # WebRTC-leg node both request it), and we need every
                # fetch to resolve to the same join_conference SWML. The
                # earlier single-use semantic caused the 2nd fetch to fall
                # through with "Agent ID required" → hangup, dropping the
                # call ~10s after the agent accepted. The 5-min TTL on
                # the Redis entry handles cleanup. join_conference is
                # idempotent — returning the same SWML twice from the
                # same call_id is a no-op on SignalWire's side.
            except json.JSONDecodeError:
                logger.error(f"Failed to parse Redis data for token {join_token}")

    # ---------------------------------------------------------------
    # TAKEOVER MODE: If the token was a takeover request, return SWML
    # that connects the agent directly to the customer's existing call
    # using connect verb with call:{sid}
    # ---------------------------------------------------------------
    if parsed_params.get('type') == 'takeover':
        call_sid = parsed_params.get('call_sid')
        call_id_val = parsed_params.get('call_id')
        leg_id = parsed_params.get('leg_id')
        takeover_user_id = parsed_params.get('user_id')

        logger.info(f"TAKEOVER MODE: Agent {takeover_user_id} taking over call {call_id_val} (SID: {call_sid})")

        if not call_sid:
            logger.error("Takeover token missing call_sid")
            return jsonify({
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {"play": {"url": "say:Takeover data is invalid. Please try again."}},
                        "hangup"
                    ]
                }
            })

        # Find and end the AI leg via execute_rpc
        ai_call_sid = None
        if call_id_val:
            ai_leg = CallLeg.query.filter_by(
                call_id=call_id_val,
                leg_type='ai_agent',
                status='active'
            ).first()
            if ai_leg:
                ai_call_sid = getattr(ai_leg, 'signalwire_sid', None)
                ai_leg.end_leg(reason='agent_takeover')
                logger.info(f"Ended AI leg {ai_leg.id} (signalwire_sid: {ai_call_sid})")

            # Activate the human agent leg (created in initiate_takeover as 'connecting')
            if leg_id:
                human_leg = CallLeg.query.get(leg_id)
                if human_leg and human_leg.status == 'connecting':
                    human_leg.status = 'active'
                    human_leg.started_at = datetime.utcnow()
                    logger.info(f"Activated human leg {human_leg.id} for takeover")

            db.session.commit()

        # Build SWML: answer, end AI, announce to customer, connect agent, hangup
        swml_main = ["answer"]

        # End the AI leg
        if ai_call_sid:
            swml_main.append({
                "execute_rpc": {
                    "method": "end",
                    "call_id": ai_call_sid,
                    "params": {
                        "reason": "agent_takeover"
                    }
                }
            })

        # Play announcement to customer so they know a human is joining
        swml_main.append({
            "execute_rpc": {
                "method": "play",
                "call_id": call_sid,
                "params": {
                    "play": [
                        {
                            "type": "tts",
                            "params": {
                                "text": "A live agent is now joining the call."
                            }
                        }
                    ]
                }
            }
        })

        # Bridge agent to the customer's call
        swml_main.append({
            "connect": {
                "to": f"call:{call_sid}"
            }
        })

        # Explicit hangup after connect ends (customer hung up)
        swml_main.append("hangup")

        swml = {
            "version": "1.0.0",
            "sections": {
                "main": swml_main
            }
        }

        logger.debug("Returning takeover conference SWML")
        return jsonify(swml)

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
        join_type = parsed_params.get('type')  # monitor, backup, escalation, or None
        context = parsed_params.get('context')  # AI-collected context for whisper
        whisper_mode = parsed_params.get('whisper_mode')
        agent_call_sid = parsed_params.get('agent_call_sid')

        logger.info(f"Per-interaction mode: Agent {agent_id} joining conference {conference_name} (type={join_type})")
        status_callback = f"{base_url}/api/conferences/{conference_name}/status"

        # Build join_conference params based on join type
        join_params = {
            "name": conference_name,
            "status_callback": status_callback,
            "status_callback_event": "start end join leave",
        }

        if join_type == 'monitor':
            # Silent listen-only join — no beep, no impact on conference lifecycle
            join_params["muted"] = True
            join_params["beep"] = "false"
            join_params["start_on_enter"] = False
            join_params["end_on_exit"] = False
            logger.info(f"Monitor mode: silent join for agent {agent_id}")

        elif join_type == 'escalation' and whisper_mode and agent_call_sid:
            # Supervisor coach mode — hears everything, speaks only to the agent
            join_params["coach"] = agent_call_sid
            join_params["beep"] = "false"
            join_params["start_on_enter"] = False
            join_params["end_on_exit"] = False
            logger.info(f"Escalation whisper/coach mode: supervisor {agent_id} coaching agent SID {agent_call_sid}")

        elif join_type == 'whisper' and agent_call_sid:
            # Supervisor-INITIATED whisper (observer surface) — same coach
            # shape as escalation whisper, but the supervisor started it from
            # a call they're not on (call_control observe/whisper).
            join_params["coach"] = agent_call_sid
            join_params["beep"] = "false"
            join_params["start_on_enter"] = False
            join_params["end_on_exit"] = False
            logger.info(f"Observer whisper mode: supervisor {agent_id} coaching agent SID {agent_call_sid}")

        elif join_type == 'barge':
            # Supervisor-INITIATED barge (observer surface) — full
            # participant with silent entry; leaving never ends the call.
            join_params["beep"] = "false"
            join_params["start_on_enter"] = False
            join_params["end_on_exit"] = False
            logger.info(f"Observer barge mode: supervisor {agent_id} joining as full participant")

        elif join_type in ('backup', 'escalation'):
            # Backup agent or supervisor (non-whisper) — full participant, silent entry
            join_params["beep"] = "false"
            join_params["end_on_exit"] = False
            logger.info(f"{join_type.capitalize()} mode: agent {agent_id} joining as full participant")

        else:
            # Normal agent join — standard behavior
            join_params["end_on_exit"] = True
            join_params["beep"] = "onEnter"

        # Build the SWML instruction list
        swml_main = ["answer"]

        # Pre-join TTS whisper: play AI-collected context summary before joining
        if context and isinstance(context, dict):
            whisper_text = build_whisper_text(context)
            if whisper_text:
                logger.info(f"Adding pre-join whisper: {whisper_text[:100]}...")
                swml_main.append({
                    "play": {
                        "url": f"say:{whisper_text}",
                        "say_voice": "en-US-Neural2-F"
                    }
                })

        swml_main.append({"join_conference": join_params})

        swml = {
            "version": "1.0.0",
            "sections": {
                "main": swml_main
            }
        }
        logger.debug("Returning agent conference SWML")
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
                "answer",
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
    logger.debug("Returning agent conference SWML")

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
    logger.info("Join conference webhook called: %s", request_summary(request))

    # Get conference name - try multiple sources
    conference_name = request.args.get('conf') or request.args.get('conference')

    # If not in query params, parse from the 'To' parameter
    if not conference_name:
        to_param = request.form.get('To', '') or request.form.get('Called', '')
        logger.debug("Parsing conference from the To parameter")

        if '?' in to_param:
            query_string = to_param.split('?', 1)[1]
            parsed_qs = parse_qs(query_string)
            conference_name = parsed_qs.get('conf', parsed_qs.get('conference', [None]))[0]
            logger.debug("Conference name extracted from the To parameter")

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
    logger.debug("Returning join-conference SWML")

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
    logger.info("Agent join SWML endpoint called: %s", request_summary(request))

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

    logger.debug("Returning agent-join SWML for agent %s", agent_id)

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
    logger.info("Agent call state request: %s", request_summary(request, data))

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


def handle_call_conference_status(conference_name, event_type, participant_call_sid, data):
    """Handle status events for call-conf-{id} conferences.

    These are per-call conferences used in the all-conference architecture.
    Customer enters conference first, then AI is dialed in.
    When AI transfers to human, human joins same conference.

    Args:
        conference_name: e.g., "call-conf-123"
        event_type: start, end, join, leave
        participant_call_sid: The call ID of the participant
        data: Full webhook data
    """
    from app import socketio

    # Extract call_id from conference name
    try:
        call_id = int(conference_name.replace('call-conf-', ''))
    except ValueError:
        logger.error(f"Invalid call conference name: {conference_name}")
        return jsonify({'status': 'invalid_conference_name'}), 200

    call = Call.query.get(call_id)
    if not call:
        logger.error(f"Call not found for conference {conference_name}")
        return jsonify({'status': 'call_not_found'}), 200

    logger.info(f"Call conference {conference_name} event: {event_type}")

    if event_type in ['start', 'conference-start']:
        # Conference started - customer is in, now dial AI
        logger.info(f"Call conference {conference_name} started - dialing AI agent")

        # Dial AI into the conference
        try:
            from app.services.signalwire_api import SignalWireAPI
            sw_api = SignalWireAPI()
            base_url = get_base_url()

            # SWML URL that joins AI to this conference
            ai_swml_url = f"{base_url}/api/swml/ai-conference-join?conf={conference_name}&call_id={call_id}"

            logger.info(f"Dialing AI into conference via: {ai_swml_url}")

            # Create call to the AI SWML endpoint
            result = sw_api.create_call(
                to=ai_swml_url,
                swml_url=ai_swml_url,
                status_callback=signed_webhook_url(f"{base_url}/api/webhooks/call-status")
            )

            ai_call_sid = result.sid if hasattr(result, 'sid') else result.get('call_id')
            logger.info(f"AI call created: {ai_call_sid}")

            # Create AI leg record (for tracking, not for hangup management)
            # AI will self-terminate via SWAIG response or timeout
            ai_leg = CallLeg(
                call_id=call.id,
                leg_type='ai_agent',
                ai_agent_name='Receptionist',
                status='active',
                conference_name=conference_name,
                signalwire_sid=ai_call_sid
            )
            db.session.add(ai_leg)
            db.session.commit()

        except Exception as e:
            logger.error(f"Failed to dial AI into conference: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

    elif event_type in ['join', 'participant-join']:
        # Someone joined the conference
        logger.info(f"Participant {participant_call_sid} joined call conference {conference_name}")

        # Emit socket event for UI updates — to the call's workspace (§8.1)
        from app.services.ws_rooms import workspace_room
        socketio.emit('call_conference_participant_joined', {
            'conference_name': conference_name,
            'call_id': call_id,
            'participant_call_sid': participant_call_sid
        }, room=workspace_room(call.workspace_id))

    elif event_type in ['leave', 'participant-leave']:
        # Someone left the conference
        logger.info(f"Participant {participant_call_sid} left call conference {conference_name}")

        # Check if this was the AI leaving - update leg status in database
        ai_leg = CallLeg.query.filter_by(
            call_id=call_id,
            signalwire_sid=participant_call_sid,
            leg_type='ai_agent'
        ).first()
        if ai_leg:
            logger.info(f"AI agent left conference {conference_name}")
            ai_leg.status = 'ended'
            db.session.commit()

        # Emit socket event — to the call's workspace (§8.1)
        from app.services.ws_rooms import workspace_room
        socketio.emit('call_conference_participant_left', {
            'conference_name': conference_name,
            'call_id': call_id,
            'participant_call_sid': participant_call_sid
        }, room=workspace_room(call.workspace_id))

    elif event_type in ['end', 'conference-end']:
        # Conference ended - call is done
        logger.info(f"Call conference {conference_name} ended")

        # Update call status
        if call.status not in ['ended', 'completed']:
            call.update_status('ended')
            db.session.commit()

            # Emit call update
            from app.services.callcenter_socketio import emit_call_update
            emit_call_update(call)

    return jsonify({'status': 'ok'})


@conferences_bp.route('/<conference_name>/status', methods=['POST'])
def conference_status_callback(conference_name):
    """Handle SignalWire conference status callbacks.

    Events: start, end, join, leave, speak-start, speak-stop

    For call-conf-{id} conferences (all-conference architecture):
    - On 'start': Dial AI agent into the conference
    - On 'join': Track participants
    - On 'leave': Handle cleanup, check if AI should be hung up
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info(
        "Conference status callback for %s: %s",
        conference_name,
        request_summary(request, data),
    )

    event_type = data.get('event_type', data.get('StatusCallbackEvent'))
    participant_call_sid = data.get('call_id', data.get('CallSid'))

    # Handle call-conf-{id} conferences (customer call conferences)
    if conference_name.startswith('call-conf-'):
        return handle_call_conference_status(conference_name, event_type, participant_call_sid, data)

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

                # Update call status to 'active' (human handling). An agent
                # just joined the conference, so the call IS now human-handled
                # regardless of how it got here. Previously this only fired
                # for connecting/queued/ringing — which missed the AI→human
                # handoff case where the call was 'ai_active' before the
                # agent picked up, leaving the Call row stuck at handler_type
                # 'ai' and the contact page rendering AI-mode UI even though
                # a human had taken over. Flip on any non-terminal pre-active
                # status: ai_active, answered, waiting, assigned, etc.
                NON_TERMINAL_PRE_ACTIVE = {
                    'connecting', 'queued', 'ringing',
                    'ai_active', 'answered', 'waiting', 'assigned',
                    'on_hold',  # rejoin after hold flips back to active
                }
                if call.status in NON_TERMINAL_PRE_ACTIVE:
                    prev_status = call.status
                    call.handler_type = 'human'
                    call.assigned_agent_id = call.assigned_agent_id or conference.owner_user_id
                    if not call.answered_at:
                        call.answered_at = datetime.utcnow()
                    call.update_status('active')
                    logger.info(
                        f"Conference join: call {call.id} status "
                        f"{prev_status!r} → 'active', handler_type → 'human'"
                    )

                db.session.commit()

                # Emit call_update so frontend gets real-time update
                emit_call_update(call)

                # Auto-start live_translate if routing flagged this call as needing it
                # (the selected agent doesn't speak the caller's language).
                _maybe_start_live_translate(call, participant_call_sid)

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
            # RE-AUDIT-01 (2026-06-03): the previous version branched on
            # an ``is_hold_leave`` flag to skip teardown when the agent
            # left for the Hold workaround. That whole feature was
            # disabled in this round (see call_control.hold_call docstring
            # for the rationale), so this branch was both unreachable
            # AND wrong — get_active_by_call_sid filtered status='active'
            # and the hold endpoint had set status='on_hold' first, so
            # the participant row was never returned here anyway. Now
            # every leave is treated as a real leave.
            participant.leave()

            # If customer left, end the call leg and update call status.
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
                        call.update_status('completed')
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


def _require_conference_control(conference, user):
    """Return an (jsonify, status) abort tuple, or None to allow.

    ISO-6 (2026-07-07 pre-deploy): conference control routes gated only on
    @require_auth, and conference_name is derivable (``interaction-<call_sid>``),
    so any authenticated user could move/mute participants or end another
    visitor's conference. Allow only supervisors/admins or an agent who is an
    active participant in this conference.
    """
    role = getattr(user, 'role', '') or ''
    if role in SUPERVISORY_ROLES:
        return None
    part = ConferenceParticipant.query.filter_by(
        conference_id=conference.id,
        participant_type='agent',
        participant_id=str(user.id),
        status='active',
    ).first()
    if part:
        return None
    return jsonify({
        'error': "You are not a participant in this conference",
    }), 403


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

    # ISO-6: only a participant of the source conference (or supervisor/admin)
    # may move participants out of it.
    control_check = _require_conference_control(source_conference, request.current_user)
    if control_check is not None:
        return control_check

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

        # Emit events — to the conference's workspace (§8.1)
        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('participant_moved', {
            'from_conference': conference_name,
            'to_conference': target_conference_name,
            'participant_call_sid': participant_call_sid
        }, room=workspace_room(source_conference.workspace_id))

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

    # ISO-6: only a participant (or supervisor/admin) may end the conference.
    control_check = _require_conference_control(conference, request.current_user)
    if control_check is not None:
        return control_check

    try:
        # Use SignalWire API to end the conference
        from app.services.signalwire_api import SignalWireAPI
        sw_api = SignalWireAPI()
        sw_api.end_conference(conference_name)

        # Update database
        conference.end_conference()
        db.session.commit()

        # Emit event — to the conference's workspace (§8.1)
        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('conference_ended', {
            'conference_name': conference_name,
            'conference_id': conference.id
        }, room=workspace_room(conference.workspace_id))

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

    # ISO-6: resolve + authorize the conference before touching a participant.
    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        return jsonify({'error': 'Conference not found'}), 404
    control_check = _require_conference_control(conference, request.current_user)
    if control_check is not None:
        return control_check

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

    Hosted mode: follows the same outbound policy as /api/calls (Phase 2
    gate retriage replaced the old hard block) — a visitor may only dial
    their own verified number, under the per-hour cap, so the conference
    dial-out demo works without opening arbitrary PSTN dialing.
    """
    data = request.get_json()
    phone_number = data.get('phone_number')
    contact_id = data.get('contact_id')
    context = data.get('context', {})

    if not phone_number:
        return jsonify({'error': 'phone_number is required'}), 400

    current_user_id = request.current_user.id

    from app.utils.demo_config import is_demo_mode
    if is_demo_mode():
        from app.services.demo_verify import demo_outbound_denial
        denial = demo_outbound_denial(request.current_user.workspace_id, phone_number)
        if denial:
            return jsonify(denial[0]), denial[1]

    # Get or create conference record
    # For outbound calls, the conference may have been created on-the-fly
    # by the agent's Call Fabric client before this dial-out request
    conference = Conference.get_active_by_name(conference_name)
    if not conference:
        # Create the conference record for this outbound interaction
        conference = Conference(
            conference_name=conference_name,
            conference_type='interaction',
            owner_user_id=current_user_id,
            status='active',
            created_at=datetime.utcnow()
        )
        db.session.add(conference)
        db.session.commit()
        logger.info(f"Created conference record for outbound dial: {conference_name}")

    # Verify the requesting user owns this conference
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
        logger.debug("Outbound answer URL prepared: %s", urlparse(answer_url).path)
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
            conference_name=conference_name,
            created_at=datetime.utcnow()
        )
        db.session.add(call)

        # Update contact's last interaction
        if contact_id:
            from app.models import Contact
            contact = Contact.query.get(contact_id)
            if contact:
                contact.last_interaction_at = datetime.utcnow()
                contact.total_calls = (contact.total_calls or 0) + 1

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
    logger.info("Outbound-answer webhook request: %s", request_summary(request))

    # Get call info from SignalWire
    call_sid = request.form.get('CallSid') or request.form.get('call_id')
    from_number = request.form.get('From')
    to_number = request.form.get('To')

    logger.info(
        "Outbound answer call=%s from=%s to=%s",
        call_sid,
        mask_phone(from_number),
        mask_phone(to_number),
    )

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
                        "end_on_exit": True,      # End conference when customer hangs up
                        "beep": "onEnter",        # Beep when customer joins
                        "status_callback": status_callback,
                        "status_callback_event": "join leave"
                    }
                }
            ]
        }
    }

    logger.debug("Returning outbound-answer conference SWML")

    return jsonify(swml)


@conferences_bp.route('/<conference_name>/call-state', methods=['POST'])
def call_state_webhook(conference_name):
    """Handle call state events for outbound calls.

    Called by SignalWire when call state changes (created, ringing, answered, ended).
    Updates the Call record and emits socket events to update the UI.
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info(f"Call state webhook for conference {conference_name}")
    logger.info("Call state request: %s", request_summary(request, data))

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
        if new_status == 'active':
            call.handler_type = 'human'
            call.assigned_agent_id = call.assigned_agent_id or call.user_id
            call.answered_at = call.answered_at or datetime.utcnow()
        call.update_status(new_status)
        logger.info(f"Call {call.id} status: {old_status} -> {new_status}")

        db.session.commit()

        # Emit update to frontend
        emit_call_update(call)

        # Emit call_ended for terminal states (matches webhooks.py pattern)
        if new_status in ['ended', 'failed', 'completed']:
            from app import socketio
            from app.services.ws_rooms import workspace_room
            call_ended_data = {
                'callId': call.id,
                'call_sid': call.signalwire_call_sid,
                'reset_ui': True
            }
            socketio.emit('call_ended', call_ended_data,
                          room=workspace_room(call.workspace_id))

    return jsonify({'status': 'ok'})
