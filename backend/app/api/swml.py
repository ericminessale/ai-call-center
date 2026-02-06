from flask import request, jsonify
from app import db, redis_client
from app.api import swml_bp
from app.models import Call, CallLeg, WebhookEvent, User, Conference, ConferenceParticipant
from app.models.system_config import SystemConfig
from app.utils.url_utils import get_base_url
import logging
import json
import os

logger = logging.getLogger(__name__)


@swml_bp.route('/initial-call', methods=['POST'])
def initial_call():
    """Return SWML for initial call setup with transcription."""
    print("🔔 INITIAL-CALL ENDPOINT HIT!", flush=True)

    # Handle JSON data from SignalWire
    data = request.get_json() if request.is_json else request.form.to_dict()

    # Log the complete JSON received
    logger.info("="*50)
    logger.info("SWML REQUEST: /api/swml/initial-call")
    logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
    logger.info("="*50)
    print(f"🔔 Call ID: {data.get('call', {}).get('call_id')}", flush=True)

    # Extract call information from the JSON structure
    call_data = data.get('call', {})
    call_id = call_data.get('call_id')
    from_number = call_data.get('from_number')
    to_number = call_data.get('to_number')
    project_id = call_data.get('project_id')
    space_id = call_data.get('space_id')
    call_state = call_data.get('call_state')
    direction = call_data.get('direction')

    logger.info(f"Extracted - Call ID: {call_id}, From: {from_number}, To: {to_number}, State: {call_state}")

    # Store or update call in database
    call = Call.find_by_sid(call_id)
    if not call:
        # Try to find a system user or the first user for now
        from app.models import User
        system_user = User.find_by_email('system@signalwire.local')
        if not system_user:
            # Get the first user or create a system user
            system_user = db.session.query(User).first()
            if not system_user:
                # Create a system user
                system_user = User(
                    email='system@signalwire.local',
                    is_active=True
                )
                system_user.set_password('system_password_change_me')
                db.session.add(system_user)
                db.session.flush()  # Get the ID before committing

        # Look up or create contact based on from_number
        contact = None
        contact_id = None
        if from_number:
            from app.models import Contact
            contact = Contact.query.filter_by(phone=from_number).first()
            if not contact:
                # Create a new contact for unknown caller
                contact = Contact(
                    phone=from_number,
                    display_name=from_number,  # Use phone as display name initially
                    account_tier='free',
                    account_status='prospect'
                )
                db.session.add(contact)
                db.session.flush()  # Get the ID
                logger.info(f"Created new contact for {from_number}: ID {contact.id}")
            contact_id = contact.id

        # Create new call record
        # Calls coming to /initial-call are INBOUND (SignalWire calling us when someone dials our number)
        # Also set handler_type to 'ai' since we're transferring to AI agent
        call = Call(
            signalwire_call_sid=call_id,
            user_id=system_user.id,
            contact_id=contact_id,  # Link to contact
            from_number=from_number,  # Store caller's number
            destination=to_number or 'unknown',
            destination_type='phone' if (to_number and to_number.startswith('+')) else 'sip',
            direction=direction or 'inbound',  # Use direction from SignalWire, default to inbound
            handler_type='ai',  # Initial calls go to AI agent
            status=call_state or 'initiated',
            transcription_active=True
        )
        db.session.add(call)
        logger.info(f"Created new call {call_id} with from_number: {from_number}, contact_id: {contact_id}")
    else:
        # Update existing call
        call.update_status(call_state)
        # Update from_number if not already set
        if from_number and not call.from_number:
            call.from_number = from_number
            logger.info(f"Updated call {call_id} with from_number: {from_number}")

    db.session.commit()

    # Log the webhook event for debugging (after call is saved)
    # Use the call.id (primary key) not call_id (SignalWire ID) for the foreign key
    WebhookEvent.log_event(
        event_type='swml_request',
        payload=data,
        call_id=call.id if call else None
    )

    # Immediately mark call as ai_active since we're transferring to AI agent
    # This makes it appear in the Agent Dashboard as "AI Active"
    call.update_status('ai_active')

    # Create initial AI leg for tracking
    # Include AI conference info for future conference-based routing
    existing_leg = CallLeg.get_active_leg(call.id)
    if not existing_leg:
        # Get or create AI conference for this call
        ai_conference = Conference.get_or_create_ai_conference('receptionist')
        db.session.flush()

        CallLeg.create_initial_leg(
            call=call,
            leg_type='ai_agent',
            ai_agent_name='Receptionist',
            conference_id=ai_conference.id,
            conference_name=ai_conference.conference_name
        )

    db.session.commit()

    # Emit WebSocket event so frontend sees the active AI call
    from app import socketio
    call_data = {
        'call_sid': call_id,
        'signalwire_call_sid': call_id,  # Include for frontend compatibility
        'id': call.id,
        'contact_id': call.contact_id,  # Link to contact for frontend
        'phoneNumber': from_number or 'unknown',  # Show caller's number
        'from_number': from_number,  # Explicitly include for clarity
        'status': 'ai_active',  # Dashboard status
        'handler_type': 'ai',  # Explicitly mark as AI call
        'internal_status': 'ai_active',
        'destination': to_number or 'unknown',
        'destination_type': 'phone' if (to_number and to_number.startswith('+')) else 'sip',
        'transcription_active': True,
        'startTime': call.created_at.isoformat() if call.created_at else None,
        'created_at': call.created_at.isoformat() if call.created_at else None,
        'answered_at': call.answered_at.isoformat() if call.answered_at else None,
        'user_id': call.user_id,
        'queueId': 'general'
    }

    # Emit to ALL agents for AI calls (no room = broadcast to all)
    # AI calls should be visible to all agents, assigned calls go to specific rooms
    socketio.emit('call_update', {'call': call_data})
    socketio.emit('call_status', call_data)

    logger.info(f"✓ Emitted AI call to all agents: {call_id}")

    # Get the base URL for callbacks (uses EXTERNAL_URL env var if set)
    base_url = get_base_url()
    logger.info(f"Using base URL: {base_url}")

    # Read routing config from database (admin-configurable)
    initial_handler = SystemConfig.get('route.initial_handler', '/receptionist')
    sales_specialist = SystemConfig.get('route.sales_specialist', '/sales-ai')
    support_specialist = SystemConfig.get('route.support_specialist', '/support-ai')

    # Transfer to configurable AI agent with routing config in global_data
    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": [
                # Set the call state URL to receive hangup notifications
                {
                    "set": {
                        "call_state_url": f"{base_url}/api/webhooks/call-status",
                        "call_state_events": "created,ringing,answered,ended"
                    }
                },
                "answer",
                {
                    "record_call": {
                        "format": "mp3",
                        "stereo": False,
                        "beep": False,
                        "status_url": f"{base_url}/api/webhooks/recording-status"
                    }
                },
                {
                    "live_transcribe": {
                        "action": {
                            "start": {
                                "webhook": f"{base_url}/api/webhooks/transcription",
                                "lang": "en-US",
                                "live_events": True,
                                "ai_summary": True,
                                "direction": ["remote-caller", "local-caller"]
                            }
                        }
                    }
                },
                # Transfer to admin-configured AI agent
                {
                    "transfer": {
                        "dest": f"{base_url}{initial_handler}"
                    }
                }
            ]
        }
    }

    # Log the SWML response
    logger.info("="*50)
    logger.info("SWML RESPONSE: /api/swml/initial-call")
    logger.info(f"JSON: {json.dumps(swml_response, indent=2)}")
    logger.info("="*50)

    return jsonify(swml_response)


@swml_bp.route('/start-transcription', methods=['POST'])
def start_transcription():
    """Return SWML to start live transcription."""
    call_sid = request.form.get('CallSid')
    logger.info(f"Start transcription SWML requested for: {call_sid}")

    # Update call in database
    call = Call.find_by_sid(call_sid)
    if call:
        call.transcription_active = True
        db.session.commit()

    base_url = get_base_url()

    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "live_transcribe": {
                        "action": {
                            "start": {
                                "webhook": f"{base_url}/api/webhooks/transcription",
                                "lang": "en-US",
                                "live_events": True,
                                "partial_events": False,
                                "direction": ["remote-caller"],
                                "beep": True,
                                "timeout": 30,
                                "hints": ["SignalWire", "transcription", "voice"]
                            }
                        }
                    }
                },
                {
                    "play": {
                        "urls": [
                            "silence: 7200"
                        ]
                    }
                }
            ]
        }
    })


@swml_bp.route('/stop-transcription', methods=['POST'])
def stop_transcription():
    """Return SWML to stop live transcription."""
    call_sid = request.form.get('CallSid')
    logger.info(f"Stop transcription SWML requested for: {call_sid}")

    # Update call in database
    call = Call.find_by_sid(call_sid)
    if call:
        call.transcription_active = False
        db.session.commit()

    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "live_transcribe": {
                        "action": {
                            "stop": {}
                        }
                    }
                },
                {
                    "play": {
                        "urls": [
                            "silence: 7200"
                        ]
                    }
                }
            ]
        }
    })


@swml_bp.route('/summarize-transcription', methods=['POST'])
def summarize_transcription():
    """Return SWML to request transcription summary."""
    call_sid = request.form.get('CallSid')
    logger.info(f"Summarize transcription SWML requested for: {call_sid}")

    base_url = get_base_url()

    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                {
                    "live_transcribe": {
                        "action": {
                            "summarize": {
                                "webhook": f"{base_url}/api/webhooks/summary"
                            }
                        }
                    }
                },
                {
                    "play": {
                        "urls": [
                            "silence: 7200"
                        ]
                    }
                }
            ]
        }
    })


@swml_bp.route('/end-call', methods=['POST'])
def end_call():
    """Return SWML to end the call."""
    call_sid = request.form.get('CallSid')
    logger.info(f"End call SWML requested for: {call_sid}")

    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                "hangup"
            ]
        }
    })


@swml_bp.route('/takeover/<token>', methods=['POST'])
def takeover_swml(token):
    """Return SWML to bridge an agent into an existing AI call.

    DIRECT CONNECT APPROACH:
    Uses SWML connect verb with call:{call_sid} to bridge the agent
    directly to the customer's existing call. The AI leg is ended via
    execute_rpc in the SWML before connecting.

    This is simpler than conferences - agent connects directly to customer.
    """
    logger.info(f"TAKEOVER SWML requested with token: {token[:8]}...")

    # Look up the token in Redis
    takeover_data = redis_client.get(f'takeover:{token}')

    if not takeover_data:
        logger.error(f"Takeover token not found or expired: {token[:8]}...")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    "answer",
                    {
                        "play": {
                            "urls": ["say:Sorry, this takeover link has expired. Please try again."]
                        }
                    },
                    "hangup"
                ]
            }
        }), 200

    # Delete the token (one-time use)
    redis_client.delete(f'takeover:{token}')

    # Parse the takeover data
    data = json.loads(takeover_data)
    original_call_sid = data['call_sid']
    call_id = data['call_id']
    leg_id = data['leg_id']
    user_id = data.get('user_id')

    logger.info(f"Takeover: Agent {user_id} taking over call {call_id} (SID: {original_call_sid})")

    # Get the call
    call = Call.query.filter_by(id=call_id).first()
    if not call:
        logger.error(f"Call not found: {call_id}")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"urls": ["say:Call not found. Please try again."]}},
                    "hangup"
                ]
            }
        }), 200

    # Update the human leg status
    leg = db.session.query(CallLeg).filter_by(id=leg_id).first()
    if leg:
        leg.status = 'active'

    # Update call handler type
    call.handler_type = 'human'
    call.user_id = user_id
    db.session.commit()

    # Find the AI leg to end it via execute_rpc
    ai_leg = CallLeg.query.filter_by(
        call_id=call_id,
        leg_type='ai_agent',
        status='active'
    ).first()

    ai_call_sid = ai_leg.signalwire_sid if ai_leg else None
    if ai_leg:
        ai_leg.status = 'ended'
        db.session.commit()
        logger.info(f"Will end AI leg via execute_rpc: {ai_call_sid}")

    # Emit WebSocket event
    from app import socketio
    socketio.emit('call_takeover_connecting', {
        'call_sid': original_call_sid,
        'call_id': call_id,
        'leg_id': leg_id
    }, room=f'call_{original_call_sid}')

    # Build SWML response:
    # 1. Answer the agent's call
    # 2. End the AI leg via execute_rpc (if we have its SID)
    # 3. Connect agent directly to the customer's call
    swml_main = ["answer"]

    # End the AI leg if we have its call SID
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

    # Connect to the customer's call
    swml_main.append({
        "connect": {
            "to": f"call:{original_call_sid}"
        }
    })

    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": swml_main
        }
    }

    logger.info(f"TAKEOVER SWML RESPONSE: {json.dumps(swml_response, indent=2)}")

    return jsonify(swml_response)


