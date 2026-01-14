"""
Queue Management API Endpoints
Handles call queuing, agent assignment, and queue monitoring
"""

from flask import Blueprint, jsonify, request, current_app
from app.services.queue_service import QueueService
from app.services.redis_service import get_redis_client
from app.services.callcenter_socketio import emit_call_update
from app.utils.decorators import require_auth
from app.utils.url_utils import get_base_url
from app import db
from app.models import Call, User, Conference, ConferenceParticipant, CallLeg
from datetime import datetime
import logging
import json
import os

logger = logging.getLogger(__name__)

queues_bp = Blueprint('queues', __name__)

# Initialize queue service
queue_service = None


def get_queue_service():
    """Get or create queue service instance"""
    global queue_service
    if queue_service is None:
        redis_client = get_redis_client()
        queue_service = QueueService(redis_client)
    return queue_service


@queues_bp.route('/<queue_id>/route', methods=['POST'])
def route_call_to_queue(queue_id):
    """
    Route an incoming call to a queue
    Called by AI agents via SWML transfer
    Returns SWML to place caller on hold while waiting for agent
    """
    try:
        print(f"🎯 QUEUE ROUTE HIT: /api/queues/{queue_id}/route", flush=True)
        data = request.json or {}
        logger.info(f"Queue route received data: {json.dumps(data, default=str)[:500]}")
        print(f"📥 Queue route data: {json.dumps(data, default=str)[:300]}", flush=True)

        # Extract call information from SignalWire webhook
        # SignalWire sends call info nested under 'call' key
        call_data = data.get('call', {})
        call_id = call_data.get('call_id') or data.get('CallSid') or data.get('call_id')
        caller_number = call_data.get('from_number') or data.get('From') or data.get('caller_number')

        # Get context from AI agent (passed via headers or body)
        context = {
            'customer_name': data.get('customer_name'),
            'account_number': data.get('account_number'),
            'issue_description': data.get('issue_description'),
            'priority': data.get('priority', 5),
            'ai_summary': data.get('ai_summary'),
            'global_data': data.get('global_data', {})
        }

        # Clean up None values
        context = {k: v for k, v in context.items() if v is not None}

        # Get priority from context or default
        priority = context.get('priority', 5)

        # Create or update call record in database
        call = Call.query.filter_by(signalwire_call_sid=call_id).first() if call_id else None
        if not call:
            # Try to find existing call, or get system user for new calls
            system_user = User.query.filter_by(email='system@signalwire.local').first()
            if not system_user:
                system_user = db.session.query(User).first()
                if not system_user:
                    # Create system user
                    system_user = User(
                        email='system@signalwire.local',
                        is_active=True
                    )
                    system_user.set_password('system_password_change_me')
                    db.session.add(system_user)
                    db.session.flush()

            call = Call(
                signalwire_call_sid=call_id,
                user_id=system_user.id,
                from_number=caller_number,
                destination=call_data.get('to_number') or data.get('To'),
                status='queued',
                destination_type='phone',
                handler_type='human',
                created_at=datetime.utcnow()
            )
            db.session.add(call)

        # Store AI context (customer info collected by AI agent)
        call.ai_context = json.dumps(context) if context else None
        db.session.commit()

        # Enqueue the call
        service = get_queue_service()
        queue_result = service.enqueue_call(
            call_id=call_id,
            queue_id=queue_id,
            priority=priority,
            context=context,
            caller_info={
                'number': caller_number,
                'name': context.get('customer_name')
            }
        )

        # Check for available agents
        available_agents = service.get_available_agents(queue_id)
        redis_client = get_redis_client()

        # Sort for consistent round-robin ordering (Redis sets are unordered)
        available_agents = sorted(available_agents) if available_agents else []

        if available_agents:
            # Round-robin selection: track last agent index in Redis
            rr_key = f"round_robin:{queue_id}"
            last_index_raw = redis_client.get(rr_key)
            last_index = int(last_index_raw) if last_index_raw else -1

            # Try each agent in round-robin order until we find one with Call Fabric
            selected_user = None
            attempts = 0
            num_agents = len(available_agents)

            while attempts < num_agents:
                next_index = (last_index + 1 + attempts) % num_agents
                agent_id_str = available_agents[next_index]

                logger.info(f"Round-robin attempt {attempts + 1}: checking agent {agent_id_str} (index {next_index})")

                # Look up user by ID (agent_id is stored as string in Redis)
                try:
                    agent_id = int(agent_id_str)
                    user = User.query.filter_by(id=agent_id).first()
                except (ValueError, TypeError):
                    # If not numeric, try lookup by email
                    user = User.query.filter_by(email=agent_id_str).first()

                if user and user.signalwire_address:
                    selected_user = user
                    # Update round-robin index to this agent
                    redis_client.set(rr_key, next_index)
                    break
                else:
                    logger.warning(f"Agent {agent_id_str} has no signalwire_address, trying next")
                    attempts += 1

            if selected_user:
                # Dequeue the call for this agent
                dequeued_data = service.dequeue_call(queue_id, str(selected_user.id))

                # Update call record
                if call:
                    call.status = 'connecting'
                    call.handler_type = 'human'
                    call.user_id = selected_user.id

                # Get base URL for callbacks (uses EXTERNAL_URL env var if set)
                base_url = get_base_url()

                # Conference-based routing: customer joins agent's conference (hot seat model)
                conference_name = f"agent-conf-{selected_user.id}"

                logger.info(f"Routing call {call_id} to agent {selected_user.email} via conference: {conference_name}")

                # Get or create the conference
                conference = Conference.get_or_create_agent_conference(selected_user.id)

                # Create call leg for human agent
                if call:
                    CallLeg.create_next_leg(
                        call=call,
                        leg_type='human_agent',
                        user_id=selected_user.id,
                        conference_id=conference.id,
                        conference_name=conference_name,
                        transition_reason='queue_routing'
                    )

                db.session.commit()

                # Emit call_update so frontend immediately knows this is now a human-handled call
                emit_call_update(call)
                logger.info(f"Emitted call_update for call {call.id} (handler_type={call.handler_type}, status={call.status})")

                # Emit WebSocket event for agent notification
                # Room name must match authenticate handler: str(user_id)
                from app import socketio
                socketio.emit('customer_routed_to_conference', {
                    'call_id': call_id,
                    'call_db_id': call.id if call else None,
                    'caller_number': caller_number,
                    'queue_id': queue_id,
                    'context': context,
                    'agent_id': selected_user.id,
                    'agent_name': selected_user.name or selected_user.email,
                    'conference_name': conference_name,
                    'customer_info': {
                        'phone': caller_number
                    }
                }, room=str(selected_user.id))
                logger.info(f"Emitted customer_routed_to_conference to agent room {selected_user.id}")

                # Return SWML response to connect customer to agent
                #
                # IMPORTANT: Call Fabric addressing for SWML connect
                # - Phone numbers: "+1234567890"
                # - SIP endpoints: "sip:user@domain.com"
                # - SWML/webhook URLs: "https://example.com/swml"
                #
                # The /private/email format does NOT work with SWML connect.
                # Instead, we transfer to a SWML URL that handles the agent connection.
                # The agent must be dialed via their subscriber's SIP address or
                # through a conference-based approach.

                # Build dial target for the agent
                # Options (in order of preference):
                # 1. signalwire_address if it's valid format (no email @)
                # 2. Derive from user ID: /private/agent-{user_id}
                # 3. SIP or phone number fallback
                #
                # IMPORTANT: Call Fabric addresses must be alphanumeric with hyphens.
                # Format like "/private/eric.minessale@gmail.com" is INVALID.
                # Valid format: "/private/agent-4" or "/private/ericminessale"

                transfer_target = None

                # Option 1: Check if signalwire_address is already in valid format
                if selected_user.signalwire_address:
                    addr = selected_user.signalwire_address
                    # Valid if: starts with /private/ or /public/ AND no @ in the name part
                    # Or is a phone number or SIP URI
                    if addr.startswith('/private/') or addr.startswith('/public/'):
                        # Extract the name part and validate
                        name_part = addr.split('/')[-1]
                        if '@' not in name_part:
                            transfer_target = addr
                            logger.info(f"Using signalwire_address for agent: {transfer_target}")
                        else:
                            logger.warning(f"Invalid fabric address format (contains @): {addr}")
                            # Fix: Use derived address instead
                            transfer_target = f"/private/agent-{selected_user.id}"
                            logger.info(f"Using derived fabric address: {transfer_target}")
                            # Update the user's stored address for future calls
                            selected_user.signalwire_address = transfer_target
                            db.session.commit()
                    elif addr.startswith('+') or addr.startswith('sip:'):
                        transfer_target = addr
                        logger.info(f"Using phone/SIP for agent: {transfer_target}")

                # Option 2: Derive from user ID if no valid address
                if not transfer_target and selected_user.signalwire_subscriber_id:
                    transfer_target = f"/private/agent-{selected_user.id}"
                    logger.info(f"Using derived fabric address: {transfer_target}")

                # Conference-based routing: customer joins agent's conference
                # The agent is already in their conference (hot seat mode)
                # We route the customer INTO that conference
                logger.info(f"Routing customer to agent {selected_user.email} conference: {conference_name}")

                # Return SWML that joins the customer to the agent's conference
                return jsonify({
                    "version": "1.0.0",
                    "sections": {
                        "main": [
                            {
                                "play": {
                                    "url": "say:Connecting you to a specialist now."
                                }
                            },
                            {
                                "join_conference": {
                                    "name": conference_name
                                }
                            }
                        ]
                    }
                })
            else:
                # No agents with valid Call Fabric addresses
                logger.warning(f"No available agents with Call Fabric addresses for queue {queue_id}")

        # No agents available - place in queue with hold message
        logger.info(f"Call {call_id} queued at position {queue_result['position']}")

        # Check how long the caller has been waiting
        wait_time_seconds = 0
        if call and call.created_at:
            wait_time_seconds = (datetime.utcnow() - call.created_at).total_seconds()

        # After 2 minutes, offer to go back to AI
        MAX_WAIT_BEFORE_AI_OFFER = 120  # 2 minutes
        offer_ai_fallback = wait_time_seconds > MAX_WAIT_BEFORE_AI_OFFER

        logger.info(f"Call {call_id} wait time: {wait_time_seconds:.0f}s, offer AI: {offer_ai_fallback}")

        # Get base URL for callbacks (uses EXTERNAL_URL env var if set)
        base_url = get_base_url()

        # Build appropriate SWML response based on wait time
        if offer_ai_fallback:
            # Offer AI fallback after waiting too long
            # Map queue_id to appropriate AI agent
            ai_agent_map = {
                'sales': 'sales-ai',
                'support': 'support-ai',
                'billing': 'support-ai'  # Billing uses support AI
            }
            ai_agent = ai_agent_map.get(queue_id, 'receptionist')

            swml_response = {
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {
                            "play": {
                                "url": f"say:We apologize for the extended wait. "
                                       f"All our specialists are still assisting other customers. "
                                       f"Let me connect you with our AI assistant who may be able to help you right away."
                            }
                        },
                        # Transfer to AI agent
                        {
                            "transfer": {
                                "dest": f"{base_url}/{ai_agent}"
                            }
                        }
                    ]
                }
            }
            logger.info(f"Transferring call {call_id} to AI fallback: {ai_agent}")
        else:
            # Normal hold message
            swml_response = {
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {
                            "play": {
                                "url": f"say:All of our specialists are currently helping other customers. "
                                       f"You are number {queue_result['position']} in the queue. "
                                       f"Please hold and an agent will be with you shortly."
                            }
                        },
                        # Play silence for 30 seconds, then check for agents again
                        {
                            "play": {
                                "url": "silence:30"
                            }
                        },
                        {
                            "play": {
                                "url": "say:Thank you for your patience. You are still in the queue."
                            }
                        },
                        # Transfer back to queue check (creates a loop)
                        {
                            "transfer": {
                                "dest": f"{base_url}/api/queues/{queue_id}/route"
                            }
                        }
                    ]
                }
            }

        print(f"📤 Returning SWML (no agents, AI fallback={offer_ai_fallback}): {json.dumps(swml_response)}", flush=True)
        return jsonify(swml_response)

    except Exception as e:
        logger.error(f"Error routing call to queue {queue_id}: {str(e)}")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [{
                    "play": {
                        "url": "say:We're experiencing technical difficulties. Please try again later."
                    }
                }, {
                    "hangup": {}
                }]
            }
        }), 500


@queues_bp.route('/<queue_id>/hold-menu', methods=['POST'])
def queue_hold_menu(queue_id):
    """
    Hold menu with DTMF options for callers waiting in queue.
    Options:
    - Press 1: Speak with AI specialist
    - Press 2: Request callback
    - Press 3: Stay on hold
    """
    try:
        data = request.json or {}
        call_data = data.get('call', {})
        call_id = call_data.get('call_id') or data.get('CallSid') or data.get('call_id')

        logger.info(f"Hold menu for call {call_id} in queue {queue_id}")

        # Get base URL for callbacks (uses EXTERNAL_URL env var if set)
        base_url = get_base_url()

        # Map queue_id to AI agent
        ai_agent_map = {
            'sales': 'sales-ai',
            'support': 'support-ai',
            'billing': 'support-ai'
        }
        ai_agent = ai_agent_map.get(queue_id, 'support-ai')

        # Build DTMF menu with prompt
        swml_response = {
            "version": "1.0.0",
            "sections": {
                "main": [
                    {
                        "prompt": {
                            "play": f"say:While you wait, you have options. "
                                   f"Press 1 to speak with our AI specialist who can help right away. "
                                   f"Press 2 to request a callback when an agent is available. "
                                   f"Press 3 or stay on the line to continue waiting.",
                            "speech": {
                                "timeout": 10,
                                "end_silence_timeout": 1
                            },
                            "digits": {
                                "max_digits": 1,
                                "digit_timeout": 10
                            }
                        }
                    },
                    # Handle the response with switch
                    {
                        "switch": {
                            "variable": "prompt_value",
                            "case": {
                                "1": [
                                    {
                                        "play": {
                                            "url": "say:Connecting you with our AI specialist."
                                        }
                                    },
                                    {
                                        "transfer": {
                                            "dest": f"{base_url}/{ai_agent}"
                                        }
                                    }
                                ],
                                "2": [
                                    {
                                        "play": {
                                            "url": "say:We have added you to our callback list. "
                                                   "An agent will call you back as soon as one becomes available. "
                                                   "Thank you for calling. Goodbye."
                                        }
                                    },
                                    # TODO: Implement callback registration
                                    "hangup"
                                ],
                                "3": [
                                    # Stay on hold - go to hold loop
                                    {
                                        "transfer": {
                                            "dest": f"{base_url}/api/queues/{queue_id}/hold-loop"
                                        }
                                    }
                                ]
                            },
                            "default": [
                                # No input or invalid - go to hold loop
                                {
                                    "transfer": {
                                        "dest": f"{base_url}/api/queues/{queue_id}/hold-loop"
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }

        return jsonify(swml_response)

    except Exception as e:
        logger.error(f"Error in hold menu: {str(e)}")
        base_url = get_base_url()
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {
                        "play": {
                            "url": "say:Please hold while we connect you."
                        }
                    },
                    {
                        "transfer": {
                            "dest": f"{base_url}/api/queues/{queue_id}/route"
                        }
                    }
                ]
            }
        })


@queues_bp.route('/<queue_id>/hold-loop', methods=['POST'])
def queue_hold_loop(queue_id):
    """
    Hold loop - plays hold music/messages and periodically checks for available agents.
    """
    try:
        data = request.json or {}
        call_data = data.get('call', {})
        call_id = call_data.get('call_id') or data.get('CallSid') or data.get('call_id')

        logger.info(f"Hold loop for call {call_id} in queue {queue_id}")

        # Get base URL (uses EXTERNAL_URL env var if set)
        base_url = get_base_url()

        # Check queue position
        service = get_queue_service()
        queue_status = service.get_queue_status(queue_id)
        position = queue_status.get('length', 0)

        # Build hold loop SWML
        swml_response = {
            "version": "1.0.0",
            "sections": {
                "main": [
                    {
                        "play": {
                            "url": f"say:Thank you for your patience. "
                                   f"You are currently number {max(position, 1)} in the queue. "
                                   f"An agent will be with you shortly."
                        }
                    },
                    # Play hold music (using silence for now, could be music URL)
                    {
                        "play": {
                            "url": "silence:20"
                        }
                    },
                    {
                        "play": {
                            "url": "say:We appreciate your patience. Please continue to hold."
                        }
                    },
                    # Play more hold time
                    {
                        "play": {
                            "url": "silence:20"
                        }
                    },
                    # Check for agent again by transferring to route
                    {
                        "transfer": {
                            "dest": f"{base_url}/api/queues/{queue_id}/route"
                        }
                    }
                ]
            }
        }

        return jsonify(swml_response)

    except Exception as e:
        logger.error(f"Error in hold loop: {str(e)}")
        base_url = get_base_url()
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {
                        "play": {
                            "url": "silence:30"
                        }
                    },
                    {
                        "transfer": {
                            "dest": f"{base_url}/api/queues/{queue_id}/route"
                        }
                    }
                ]
            }
        })


@queues_bp.route('/<queue_id>/next', methods=['GET'])
@require_auth
def get_next_queued_call(queue_id):
    """
    Agent requests the next call from their queue
    """
    try:
        # Get agent ID from authenticated user
        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()

        # Set agent as available if not already
        service.set_agent_status(agent_id, "available")

        # Dequeue next call
        call_data = service.dequeue_call(queue_id, agent_id)

        if not call_data:
            return jsonify({"message": "No calls in queue"}), 204

        # Update call record
        call = Call.query.filter_by(signalwire_call_sid=call_data['call_id']).first()
        if call:
            call.status = 'in-progress'
            db.session.commit()

        logger.info(f"Agent {agent_id} took call {call_data['call_id']} from queue {queue_id}")

        return jsonify(call_data)

    except Exception as e:
        logger.error(f"Error getting next call from queue: {str(e)}")
        return jsonify({"error": "Failed to get next call"}), 500


@queues_bp.route('/<queue_id>/status', methods=['GET'])
@require_auth
def get_queue_status(queue_id):
    """
    Get current queue statistics
    """
    try:
        service = get_queue_service()
        status = service.get_queue_status(queue_id)
        metrics = service.get_queue_metrics(queue_id)

        return jsonify({
            **status,
            **metrics
        })

    except Exception as e:
        logger.error(f"Error getting queue status: {str(e)}")
        return jsonify({"error": "Failed to get queue status"}), 500


@queues_bp.route('/agent/status', methods=['PUT'])
@require_auth
def update_agent_status():
    """
    Update agent's availability status
    """
    try:
        data = request.json
        new_status = data.get('status')

        if new_status not in ['available', 'busy', 'break', 'offline']:
            return jsonify({"error": "Invalid status"}), 400

        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()
        current_call_id = data.get('current_call_id')

        service.set_agent_status(agent_id, new_status, current_call_id)

        # If going available, check for queued calls
        next_call = None
        if new_status == 'available':
            # Check all configured queues
            for queue_id in ['sales', 'support', 'billing']:
                call_data = service.dequeue_call(queue_id, agent_id)
                if call_data:
                    next_call = call_data
                    break

        logger.info(f"Agent {agent_id} status changed to {new_status}")

        return jsonify({
            "status": new_status,
            "next_call": next_call
        })

    except Exception as e:
        logger.error(f"Error updating agent status: {str(e)}")
        return jsonify({"error": "Failed to update status"}), 500


@queues_bp.route('/agent/metrics', methods=['GET'])
@require_auth
def get_agent_metrics():
    """
    Get performance metrics for the current agent
    """
    try:
        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        period_hours = request.args.get('period_hours', 24, type=int)

        service = get_queue_service()
        metrics = service.get_agent_metrics(agent_id, period_hours)

        # Add database metrics
        from sqlalchemy import func
        from datetime import timedelta

        since = datetime.utcnow() - timedelta(hours=period_hours)

        # For now, return mock metrics since we don't have agent_id on calls
        calls_handled = 15
        avg_duration = 240

        metrics.update({
            'calls_handled': calls_handled,
            'average_handle_time': avg_duration
        })

        return jsonify(metrics)

    except Exception as e:
        logger.error(f"Error getting agent metrics: {str(e)}")
        return jsonify({"error": "Failed to get metrics"}), 500


@queues_bp.route('/transfer', methods=['POST'])
@require_auth
def transfer_call():
    """
    Transfer a call to another agent or queue
    """
    try:
        data = request.json
        call_id = data.get('call_id')
        target = data.get('target')  # agent_id or queue_id
        transfer_type = data.get('type', 'blind')  # blind or warm

        if not call_id or not target:
            return jsonify({"error": "Missing required fields"}), 400

        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()
        result = service.transfer_call(call_id, agent_id, target, transfer_type)

        if not result['success']:
            return jsonify(result), 400

        # Update call record
        call = Call.query.filter_by(signalwire_call_sid=call_id).first()
        if call:
            # Store transfer history as JSON string
            import json
            transfer_history = json.loads(call.transfer_history or '[]')
            transfer_history.append({
                'from': agent_id,
                'to': target,
                'type': transfer_type,
                'timestamp': datetime.utcnow().isoformat()
            })
            call.transfer_history = json.dumps(transfer_history)
            db.session.commit()

        logger.info(f"Call {call_id} transferred from {agent_id} to {target}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"Error transferring call: {str(e)}")
        return jsonify({"error": "Failed to transfer call"}), 500


@queues_bp.route('/all/status', methods=['GET'])
@require_auth
def get_all_queues_status():
    """
    Get status of all queues
    """
    try:
        redis_client = get_redis_client()
        if not redis_client:
            return jsonify({"error": "Redis not available"}), 503

        # Define available queues
        queue_ids = ['sales', 'support', 'billing']

        all_status = []
        for queue_id in queue_ids:
            queue_key = f"queue:{queue_id}"
            queue_depth = redis_client.zcard(queue_key)

            # Calculate wait times if there are calls
            calls = redis_client.zrange(queue_key, 0, -1)
            wait_times = []
            now = datetime.utcnow()

            for call_json in calls:
                try:
                    call_data = json.loads(call_json)
                    enqueued = datetime.fromisoformat(call_data.get('enqueued_at', now.isoformat()))
                    wait_times.append((now - enqueued).total_seconds())
                except:
                    continue

            avg_wait = sum(wait_times) / len(wait_times) if wait_times else 0
            longest_wait = max(wait_times) if wait_times else 0

            all_status.append({
                'queue_id': queue_id,
                'name': queue_id.capitalize(),
                'depth': queue_depth,
                'average_wait_seconds': int(avg_wait),
                'longest_wait_seconds': int(longest_wait)
            })

        return jsonify(all_status)

    except Exception as e:
        logger.error(f"Error getting all queues status: {str(e)}")
        return jsonify({"error": "Failed to get queues status"}), 500


@queues_bp.route('/mock/clear', methods=['POST'])
@require_auth
def clear_mock_data():
    """
    Clear all mock/demo calls from queues
    """
    try:
        service = QueueService()
        cleared_count = 0

        # Clear demo calls from all queues
        for queue_id in ['sales', 'support', 'billing']:
            queue_key = f"queue:{queue_id}"
            redis_client = service.redis_client

            if redis_client:
                # Get all calls in the queue
                calls = redis_client.zrange(queue_key, 0, -1)

                # Remove only demo/mock calls
                for call_json in calls:
                    try:
                        call_data = json.loads(call_json)
                        call_id = call_data.get('call_id', '')

                        # Check if it's a demo call (starts with demo_ or mock_)
                        if call_id.startswith('demo_') or call_id.startswith('mock_'):
                            redis_client.zrem(queue_key, call_json)
                            cleared_count += 1
                    except Exception as e:
                        logger.warning(f"Error processing call data: {e}")

        logger.info(f"Cleared {cleared_count} mock calls from queues")

        return jsonify({
            'success': True,
            'message': f'Cleared {cleared_count} mock calls from queues',
            'cleared_count': cleared_count
        })

    except Exception as e:
        logger.error(f"Error clearing mock data: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/mock/generate', methods=['POST'])
@require_auth
def generate_mock_data():
    """
    Generate mock queue data for demos
    """
    try:
        import random
        import json
        import uuid

        # Try to import Faker, fall back to simple generation if not available
        try:
            from faker import Faker
            fake = Faker()
        except ImportError:
            fake = None

        redis_client = get_redis_client()

        if not redis_client:
            logger.error("Redis client not available")
            return jsonify({"error": "Redis not available"}), 503

        # Clear existing queue data
        for queue_id in ['sales', 'support', 'billing']:
            redis_client.delete(f"queue:{queue_id}")

        # Queue configurations for realistic demo data
        queue_configs = {
            'sales': {
                'min_calls': 3,
                'max_calls': 8,
                'vip_chance': 0.2,
                'reasons': ['Product demo request', 'Pricing inquiry', 'Enterprise upgrade', 'New customer onboarding'],
                'ai_summaries': [
                    'Customer interested in enterprise plan, needs 50+ seats',
                    'Comparing us with Twilio, wants to see AI features',
                    'Existing customer wants to add more agents',
                    'Startup looking for affordable solution'
                ]
            },
            'support': {
                'min_calls': 5,
                'max_calls': 12,
                'vip_chance': 0.15,
                'reasons': ['Technical issue', 'Integration help', 'API question', 'Billing problem', 'Feature request'],
                'ai_summaries': [
                    'WebSocket connection dropping intermittently',
                    'Need help with SWML configuration',
                    'Questions about AI agent capabilities',
                    'Call recording not working properly',
                    'Request for bulk SMS feature'
                ]
            },
            'billing': {
                'min_calls': 2,
                'max_calls': 5,
                'vip_chance': 0.25,
                'reasons': ['Payment failed', 'Invoice question', 'Plan upgrade', 'Refund request'],
                'ai_summaries': [
                    'Credit card declined, needs to update payment method',
                    'Questions about usage charges this month',
                    'Wants to upgrade from Basic to Pro plan',
                    'Requesting refund for accidental double charge'
                ]
            }
        }

        total_calls_generated = 0

        for queue_id, config in queue_configs.items():
            num_calls = random.randint(config['min_calls'], config['max_calls'])

            for i in range(num_calls):
                # Generate realistic wait times (newer calls have shorter wait times)
                wait_minutes = random.uniform(0, 15) * (1 - i/num_calls)

                # Determine priority based on position and randomness
                if i == 0 and random.random() < 0.3:  # First call might be critical
                    priority = 'urgent'
                    priority_score = 1  # For Redis sorting
                elif random.random() < config['vip_chance']:
                    priority = 'high'
                    priority_score = 2  # VIP/High
                elif i < 2:
                    priority = 'high'
                    priority_score = 3
                else:
                    priority = random.choice(['medium', 'medium', 'medium', 'low'])
                    priority_score = 5 if priority == 'medium' else 7

                # Generate customer data
                is_vip = random.random() < config['vip_chance']
                is_returning = random.random() < 0.4

                # Pick reason and AI summary
                reason = random.choice(config['reasons'])
                ai_summary = random.choice(config['ai_summaries'])

                # Generate names and phone numbers
                if fake:
                    customer_name = fake.name()
                    phone_number = fake.phone_number()
                    call_id = f'demo_{queue_id}_{fake.uuid4()[:8]}'
                    account_num = fake.random_number(digits=8) if is_returning else None
                else:
                    # Fallback without Faker
                    first_names = ['John', 'Jane', 'Mike', 'Sarah', 'David', 'Emily', 'Robert', 'Lisa']
                    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller']
                    customer_name = f"{random.choice(first_names)} {random.choice(last_names)}"
                    phone_number = f"+1{random.randint(2000000000, 9999999999)}"
                    call_id = f'demo_{queue_id}_{uuid.uuid4().hex[:8]}'
                    account_num = random.randint(10000000, 99999999) if is_returning else None

                call_data = {
                    'call_id': call_id,
                    'queue_id': queue_id,
                    'priority': priority,
                    'context': {
                        'customer_name': customer_name,
                        'phone_number': phone_number,
                        'reason': reason,
                        'ai_summary': ai_summary,
                        'sentiment': random.choices(
                            ['positive', 'neutral', 'negative'],
                            weights=[0.3, 0.5, 0.2]
                        )[0],
                        'is_vip': is_vip,
                        'is_returning': is_returning,
                        'confidence_score': random.uniform(0.75, 0.98),
                        'extracted_info': {
                            'account_number': account_num,
                            'product_tier': random.choice(['Basic', 'Pro', 'Enterprise']) if is_returning else None,
                            'monthly_spend': random.randint(100, 5000) if is_vip else None
                        },
                        'ai_actions': [
                            {'action': 'greeting', 'result': 'completed'},
                            {'action': 'identity_verification', 'result': 'completed'},
                            {'action': 'issue_categorization', 'result': reason}
                        ]
                    },
                    'caller_info': {
                        'number': phone_number,
                        'name': customer_name
                    }
                }

                # Enqueue the call directly to Redis
                queue_key = f"queue:{queue_id}"

                # Add enqueued_at timestamp
                call_data['enqueued_at'] = datetime.utcnow().isoformat()

                # Add to Redis sorted set with priority_score as score
                redis_client.zadd(queue_key, {json.dumps(call_data): priority_score})

                total_calls_generated += 1

        # Generate some agent status data
        agent_statuses = {
            'agent_sarah': {'status': 'busy', 'current_call': 'call_123', 'queue': 'sales'},
            'agent_john': {'status': 'available', 'queue': 'support'},
            'agent_emily': {'status': 'after-call', 'queue': 'billing'},
            'agent_mike': {'status': 'available', 'queue': 'support'},
            'agent_lisa': {'status': 'break', 'queue': 'sales'}
        }

        for agent_id, status_data in agent_statuses.items():
            redis_client.hset(f'agent:{agent_id}', mapping={
                'status': status_data['status'],
                'last_update': datetime.utcnow().isoformat(),
                'queue': status_data.get('queue', 'general'),
                'current_call': status_data.get('current_call', '')
            })

        # Broadcast the update via WebSocket
        from app.services.callcenter_socketio import broadcast_queue_updates
        broadcast_queue_updates()

        logger.info(f"Generated {total_calls_generated} mock calls across queues")

        # Get queue depths for response
        queue_depths = {}
        for queue_id in queue_configs.keys():
            queue_key = f"queue:{queue_id}"
            depth = redis_client.zcard(queue_key)
            queue_depths[queue_id] = depth

        return jsonify({
            'success': True,
            'message': f'Generated {total_calls_generated} mock calls for demo',
            'queues': queue_depths
        })

    except Exception as e:
        logger.error(f"Error generating mock data: {str(e)}")
        return jsonify({"error": f"Failed to generate mock data: {str(e)}"}), 500