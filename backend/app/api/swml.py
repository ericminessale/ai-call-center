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

    # Create interaction conference for this call (conference-first architecture)
    # Conference is created now so the customer can join it after the AI phase ends
    conference_name = f"interaction-{call_id}"
    call.conference_name = conference_name

    conference = Conference.create_interaction_conference(
        call_id=call_id,
        queue_id='general'
    )
    db.session.flush()

    # Create initial AI leg for tracking
    existing_leg = CallLeg.get_active_leg(call.id)
    if not existing_leg:
        CallLeg.create_initial_leg(
            call=call,
            leg_type='ai_agent',
            ai_agent_name='Receptionist',
            conference_id=conference.id,
            conference_name=conference_name
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
        'queueId': 'general',
        'conference_name': conference_name
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

    # Conference-first architecture: connect customer to AI agent via bridge (A-leg/B-leg).
    # After AI phase ends (B-leg drops from transfer or takeover), customer falls through
    # to join_conference where human agents can join.
    ai_agent_url = f"{base_url}{initial_handler}?conf={conference_name}&call_db_id={call.id}"

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
                # Connect to AI agent via bridge (B-leg runs AI, A-leg is customer)
                # When B-leg ends (transfer complete or takeover), connect completes
                # and customer falls through to join_conference below
                {
                    "connect": {
                        "to_swml": {
                            "url": ai_agent_url
                        }
                    }
                },
                # After AI phase ends, announce and put customer in conference
                {
                    "play": {
                        "url": "say:Please hold while we connect you to an agent."
                    }
                },
                {
                    "join_conference": {
                        "name": conference_name
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


    # NOTE: Old takeover SWML endpoint removed.
    # With conference-first architecture, takeover uses the standard conference join flow.
    # The agent joins the interaction conference directly - no special SWML needed.


