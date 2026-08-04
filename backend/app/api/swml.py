from datetime import datetime
import secrets
from flask import request, jsonify
from app import db, redis_client
from app.api import swml_bp
from app.models import Call, CallLeg, WebhookEvent, User, Conference, ConferenceParticipant
from app.models.system_config import SystemConfig
from app.utils.request_logging import mask_phone, request_summary
from app.utils.url_utils import get_base_url, signed_webhook_url
from app.utils.demo_config import is_demo_mode
import logging
import json
import os
import requests as http_requests

logger = logging.getLogger(__name__)


@swml_bp.route('/initial-call', methods=['POST'])
def initial_call():
    """Return SWML for initial call setup with transcription."""
    # Handle JSON data from SignalWire
    data = request.get_json() if request.is_json else request.form.to_dict()

    logger.info("SWML REQUEST: /api/swml/initial-call call_id=%s",
                data.get('call', {}).get('call_id'))
    logger.debug("Payload shape: %s", request_summary(request, data))

    # Extract call information from the JSON structure
    call_data = data.get('call', {})
    call_id = call_data.get('call_id')
    from_number = call_data.get('from_number')
    to_number = call_data.get('to_number')
    project_id = call_data.get('project_id')
    space_id = call_data.get('space_id')
    call_state = call_data.get('call_state')
    direction = call_data.get('direction')

    logger.info(
        "Extracted call=%s from=%s to=%s state=%s",
        call_id,
        mask_phone(from_number),
        mask_phone(to_number),
        call_state,
    )

    # Hosted demo only: per-caller-ID inbound ratelimit. If this
    # number has hit its hourly cap, return a polite "try later" SWML
    # before we provision any DB rows or kick off the AI agent.
    from app.services.demo_inbound_ratelimit import (
        reject_swml as demo_reject_swml,
        should_reject_inbound,
    )
    if should_reject_inbound(from_number):
        logger.info(
            "Inbound call from %s rejected by demo ratelimit",
            mask_phone(from_number),
        )
        return jsonify(demo_reject_swml())

    # Store or update call in database
    call = Call.find_by_sid(call_id)

    # Verify-first gate (§6.1/§10.1, hosted mode only): NEW inbound calls
    # are accepted ONLY from numbers bound to a live workspace. Unverified
    # → polite reject SWML, NO Call/Contact rows (one telemetry log line +
    # a Redis counter). Outbound-originated dials hit this endpoint too,
    # but their Call row was pre-created by the initiate path, so they
    # resolve above and never reach the gate.
    # NOTE: the gate is NOT conditioned on ``direction`` — that field is
    # attacker-controlled request body, and gating on it would let a
    # spoofed ``direction: outbound`` skip verify-first and mint an
    # unverified Call/Contact row (I3). Outbound-originated dials never
    # reach here as a NEW row: the initiate path pre-creates their Call, so
    # ``call`` is already truthy above and the gate is skipped for them.
    ws_binding = None
    if not call and is_demo_mode():
        from app.services.demo_verify import (
            get_workspace_for_number,
            note_rejected_inbound,
            unverified_reject_swml,
        )
        ws_binding = get_workspace_for_number(from_number)
        if ws_binding is None:
            logger.info(
                "initial-call: rejecting inbound from unverified number (call %s)",
                call_id,
            )
            note_rejected_inbound(from_number, 'initial-call')
            return jsonify(unverified_reject_swml())

    if not call:
        owner_user_id = None
        ws_id = None
        if ws_binding is not None:
            # Verified inbound: the call (and its contact) belong to the
            # bound workspace, attributed to the visitor who paired the
            # number. All downstream privacy rides on these two columns.
            ws_id, owner_user_id = ws_binding
            logger.info(
                "initial-call: attributed call %s to workspace %s (user %s)",
                call_id, ws_id, owner_user_id,
            )
        else:
            # Clone-and-own: the synthetic system user satisfies the FK on
            # Call.user_id (nobody ever logs in as it). Hosted mode never
            # reaches this branch for inbound — the verify-first gate above
            # already rejected unbound callers.
            from app.models import User
            system_user = User.find_by_email('system@signalwire.local')
            if not system_user:
                # Get the first user or create a system user
                system_user = db.session.query(User).first()
                if not system_user:
                    system_user = User(
                        email='system@signalwire.local',
                        is_active=True
                    )
                    # Generate something unguessable rather than ship a literal.
                    system_user.set_password(secrets.token_urlsafe(32))
                    db.session.add(system_user)
                    db.session.flush()  # Get the ID before committing
            owner_user_id = system_user.id

        # Look up or create contact based on from_number — scoped to the
        # attributed workspace in hosted mode (phone is per-workspace
        # unique; an unscoped lookup could bind another tenant's contact).
        #
        # Goes through contact_directory rather than doing its own
        # query-then-insert: hosted-demo pairing seeds a contact for the
        # visitor's own number and can be committing it while this request
        # runs, so both sides have to agree on the key AND survive losing the
        # race. See that module for why a pre-check can't fix it.
        contact = None
        contact_id = None
        if from_number:
            from app.services.contact_directory import resolve_contact
            contact = resolve_contact(
                ws_id,  # None → flush stamper defaults (clone-and-own)
                from_number,
                display_name=from_number,  # phone as display name initially
                account_tier='free',
                account_status='prospect',
            )
            if contact is not None:
                db.session.flush()  # Get the ID
                contact_id = contact.id
                logger.info(
                    "Resolved contact for %s: ID %s",
                    mask_phone(from_number),
                    contact_id,
                )
            # Update last interaction timestamp
            contact.last_interaction_at = datetime.utcnow()
            contact.total_calls = (contact.total_calls or 0) + 1

        # Create new call record
        # Calls coming to /initial-call are INBOUND (SignalWire calling us when someone dials our number)
        # Also set handler_type to 'ai' since we're transferring to AI agent
        call = Call(
            signalwire_call_sid=call_id,
            user_id=owner_user_id,
            contact_id=contact_id,  # Link to contact
            from_number=from_number,  # Store caller's number
            destination=to_number or 'unknown',
            destination_type='phone' if (to_number and to_number.startswith('+')) else 'sip',
            direction=direction or 'inbound',  # Use direction from SignalWire, default to inbound
            handler_type='ai',  # Initial calls go to AI agent
            status=call_state or 'initiated',
            transcription_active=True,
            workspace_id=ws_id,  # None → flush stamper derives/defaults
        )
        db.session.add(call)
        logger.info(
            "Created new call %s with from_number=%s contact_id=%s",
            call_id,
            mask_phone(from_number),
            contact_id,
        )
    else:
        # Update existing call
        call.update_status(call_state)
        # Update from_number if not already set
        if from_number and not call.from_number:
            call.from_number = from_number
            logger.info(
                "Updated call %s with from_number=%s",
                call_id,
                mask_phone(from_number),
            )

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

    # Create initial AI leg for tracking. NOT tied to a Conference row —
    # AI legs are a transfer to the AI agent's own SWML, not a conference
    # join. The SWML returned below has no `join_conference` verb. Earlier
    # code created a phantom "ai-conf-<name>" Conference row solely to
    # satisfy the (nullable!) conference_id foreign key on CallLeg, which
    # hit a unique-constraint violation on the second AI call per agent
    # name. conference_id is nullable in the schema — let it be null for
    # AI legs. Conferences will materialize only on AI→human handoff.
    existing_leg = CallLeg.get_active_leg(call.id)
    if not existing_leg:
        CallLeg.create_initial_leg(
            call=call,
            leg_type='ai_agent',
            ai_agent_name='Receptionist',
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

    # To the call's workspace — its agents see the AI-active call (§8.1;
    # was a broadcast to every socket on the install).
    from app.services.ws_rooms import workspace_room
    ws_room = workspace_room(call.workspace_id)
    socketio.emit('call_update', {'call': call_data}, room=ws_room)
    socketio.emit('call_status', call_data, room=ws_room)

    logger.info(f"✓ Emitted AI call to workspace agents: {call_id}")

    # Get the base URL for callbacks (uses EXTERNAL_URL env var if set)
    base_url = get_base_url()
    logger.info(f"Using base URL: {base_url}")

    # Read routing config — the ai-specialist route (per phone-number mode) sets
    # _target_ai_route_override via request.environ to force a specific AI agent
    # route instead of the SystemConfig-driven default triage handler.
    initial_handler = (
        request.environ.get('_target_ai_route_override')
        or SystemConfig.get('route.initial_handler', '/receptionist')
    )
    sales_specialist = SystemConfig.get('route.sales_specialist', '/sales-ai')
    support_specialist = SystemConfig.get('route.support_specialist', '/support-ai')

    # Transfer to admin-configured AI agent
    # The caller's A-leg is transferred directly to the AI agent's SWML
    # No conference at this stage - conference is only created when AI transfers to human
    main_section: list = [
        # Set the call state URL to receive hangup notifications
        {
            "set": {
                "call_state_url": signed_webhook_url(f"{base_url}/api/webhooks/call-status"),
                "call_state_events": "created,ringing,answered,ended"
            }
        },
        "answer",
    ]

    # Verbs that run once we know a person is on the line. Built separately
    # from main_section so the AMD branch below can gate all of it.
    post_answer: list = []

    # Recording is OFF by default in DEMO_MODE — visitors might say
    # sensitive things in the public sandbox; sidesteps the consent
    # question entirely. Production-shape deployments record as before.
    if not is_demo_mode():
        # Two-party-consent states require the announcement BEFORE recording
        # starts. Toggle/message live in SystemConfig so admins can adjust
        # without a deploy.
        if SystemConfig.get('recording.consent_enabled', 'true').strip().lower() == 'true':
            consent_message = SystemConfig.get(
                'recording.consent_message',
                'This call may be recorded for quality and training purposes.',
            )
            post_answer.append({"play": {"url": f"say:{consent_message}"}})
        post_answer.append({
            "record_call": {
                "format": "mp3",
                "stereo": False,
                "beep": False,
                "status_url": signed_webhook_url(f"{base_url}/api/webhooks/recording-status"),
            }
        })

    # Diagnostic kill-switch (2026-06-11): platform live_transcribe is the
    # prime suspect in the intermittent media-fork seizure (TTS + recording +
    # transcribe delivery all die together mid-call; details in the triage
    # debug notes). Setting diagnostics.live_transcribe_enabled=false runs
    # A/B calls without the component to confirm — costs the live transcript
    # panel on those calls only. Default true = normal behavior.
    if SystemConfig.get('diagnostics.live_transcribe_enabled', 'true').strip().lower() == 'true':
        post_answer.append({
            "live_transcribe": {
                "action": {
                    "start": {
                        "webhook": signed_webhook_url(f"{base_url}/api/webhooks/transcription"),
                        "lang": "en-US",
                        "live_events": True,
                        # NO ai_summary on the AI leg. The AI post-prompt
                        # (agent.summary -> /api/webhooks/post-prompt) already
                        # summarizes the AI triage and seeds the wrap-up note; the
                        # END-of-call summary comes from the conference/human leg's
                        # live_transcribe (queue_dispatch.py) and is APPENDED to it.
                        # Enabling ai_summary here too produced a duplicate AI-leg
                        # fragment that overwrote the whole-call note (last-write-wins).
                        # For AI-only calls (no human transfer) the post-prompt IS
                        # the whole-call summary.
                        "direction": ["remote-caller", "local-caller"],
                        # VAD endpointing: ms of silence before an utterance is
                        # finalized. SignalWire default is 300ms, which splits
                        # on normal mid-sentence pauses (breaths, "um", reading
                        # a number) and newlines one sentence into several.
                        # Raise it so a sentence stays one line. Tune via grep
                        # vad_silence_ms (set in both swml.py starts + the REST
                        # start_transcription); bump higher if still splitting.
                        "vad_silence_ms": 800,
                    }
                }
            }
        })

    # Sign call_db_id so the public agent route can't be used as a confused
    # deputy to render another workspace's config (§7.1 hardening) — the
    # agent forwards this token to /api/internal/call-context, which rejects
    # any unsigned/forged call_db_id.
    from app.utils.url_utils import call_context_token
    _ctk = call_context_token(call.id)
    post_answer.append(
        # Transfer to AI agent — caller's A-leg runs the AI agent's SWML directly
        {
            "transfer": {
                "dest": f"{base_url}{initial_handler}?call_db_id={call.id}&ctk={_ctk}"
            }
        }
    )

    # Outbound dials (callback dial-outs, outbound AI) pass ?amd=1 so the AI
    # never delivers its pitch to a voicemail greeting. detect_result is set
    # by detect_machine (human | machine | fax | unknown); unknown proceeds
    # like human rather than dropping a real person. Inbound calls skip AMD
    # entirely — the caller dialed us, they're human.
    if request.args.get('amd') == '1':
        main_section.append({
            "detect_machine": {
                # Keep detecting until the greeting/beep ends so the machine
                # branch leaves its message after the beep, not over it.
                "detect_message_end": True,
            }
        })
        main_section.append({
            "switch": {
                "variable": "detect_result",
                "case": {
                    "machine": [{
                        "transfer": {
                            "dest": f"{base_url}/api/swml/voicemail-detected?call_db_id={call.id}"
                        }
                    }],
                    "fax": ["hangup"],
                },
                "default": post_answer,
            }
        })
    else:
        main_section.extend(post_answer)

    swml_response = {
        "version": "1.0.0",
        "sections": {"main": main_section},
    }

    logger.debug("SWML response prepared for /api/swml/initial-call")

    return jsonify(swml_response)


@swml_bp.route('/ai-specialist/<queue_slug>', methods=['POST'])
def ai_specialist(queue_slug):
    """SWML entry point for phone numbers that route directly to a queue's AI
    specialist (skipping the receptionist's triage). Targeted by Settings →
    Phone Numbers when admin picks target_mode='ai_specialist' for a queue.

    Falls back to the configured triage handler if the queue is missing,
    inactive, or has no ai_agent_route — avoids stranding inbound calls when
    queue config drifts.
    """
    from app.models import Queue
    queue = Queue.query.filter_by(slug=queue_slug, is_active=True).first()
    if queue and queue.ai_agent_route:
        request.environ['_target_ai_route_override'] = queue.ai_agent_route
        logger.info(
            f"AI specialist routing for queue '{queue_slug}' → {queue.ai_agent_route}"
        )
    else:
        logger.warning(
            f"AI specialist route requested for queue '{queue_slug}' but "
            f"queue/ai_agent_route missing — falling back to triage"
        )
    return initial_call()


@swml_bp.route('/voicemail-detected', methods=['POST'])
def voicemail_detected():
    """SWML branch target when AMD classifies an outbound dial as a machine.

    Stamps the call so wrap-up/reporting can distinguish voicemail from a
    real conversation, then leaves a short message after the greeting (the
    detect_machine verb in initial-call waits for message end) and hangs up.
    """
    call_db_id = request.args.get('call_db_id')
    call = Call.query.get(call_db_id) if call_db_id else None
    if call:
        try:
            ctx = call.ai_context_dict
            ctx['amd_result'] = 'machine'
            call.ai_context_dict = ctx
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning(f"voicemail-detected: failed to stamp call {call_db_id}: {e}")
        WebhookEvent.log_event(
            event_type='amd_result',
            payload={'detect_result': 'machine', 'call_db_id': call.id},
            call_id=call.id,
        )
    else:
        logger.warning(f"voicemail-detected: unknown call_db_id={call_db_id!r}")

    message = SystemConfig.get(
        'outbound.voicemail_message',
        "Hi, sorry we missed you. We'll try to reach you again soon. Goodbye.",
    )
    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                {"play": {"url": f"say:{message}"}},
                "hangup",
            ]
        },
    })


@swml_bp.route('/out-of-service', methods=['POST', 'GET'])
def out_of_service():
    """SWML target for unassigned phone numbers.

    SignalWire's ``relay_script`` call handler rejects empty
    ``call_relay_script_url`` with HTTP 422 ("Call relay script url must be
    set"). So "unassign" can't just clear the URL — every assigned number
    must point somewhere. We point unassigned numbers here, which plays a
    polite "not in service" message and hangs up.

    Our list endpoint's ``_parse_phone_routing_from_url`` does not match this
    path, so numbers pointing here correctly read as ``is_assigned: False``
    in the admin UI. The number stays on the SignalWire account (we don't
    release it) but the caller hears a clean rejection instead of an error.
    """
    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {"play": {"url": "say:This number is not currently in service. Please check the number and try again."}},
                "hangup",
            ]
        }
    })


@swml_bp.route('/queue-pickup/<queue_slug>', methods=['POST', 'GET'])
def queue_pickup(queue_slug):
    """SWML executed on an agent's leg when the backend dials them to bridge
    to the next parked caller in the named queue.

    Flow:
      1. Caller arrives, no agent available → bridge ingress places them in
         ``enter_queue: queue_name: <slug>`` (SignalWire-side queue).
      2. Agent goes available → backend issues an outbound dial: to=agent's
         /private/<addr>, with THIS URL as the dial's swml. SignalWire calls
         the agent's subscriber; their SDK rings.
      3. Agent accepts → this SWML executes on the freshly-answered leg →
         ``connect: to: queue:<slug>`` pops the next queued caller →
         SignalWire bridges the two legs. Native matchmaking, no conference.

    Endpoint is webhook-auth gated via signed URL. The signed URL is what
    we pass to ``dial_to_queue_pickup`` (signalwire_api.py).
    """
    logger.info(f"queue-pickup SWML fetched for queue '{queue_slug}'")
    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "connect": {
                        # The "queue:<name>" destination format pops the next
                        # caller from SignalWire's enter_queue. Confirmed via
                        # Sigmond KB connect-verb reference.
                        "to": f"queue:{queue_slug}",
                        # answer_on_bridge so the agent isn't billed for any
                        # tiny gap between the dial completing and the queued
                        # caller bridging in.
                        "answer_on_bridge": True,
                        # Short timeout — if SignalWire can't pop a caller
                        # quickly the queue was racy and we should fall
                        # through instead of leaving the agent on a dead leg.
                        "timeout": 10,
                    }
                },
                "hangup",
            ]
        }
    })


@swml_bp.route('/queue-wait/<queue_slug>', methods=['POST', 'GET'])
def queue_wait(queue_slug):
    """SWML returned as the ``wait_url`` for SignalWire's ``enter_queue`` verb.

    Bridge-mode parked callers hit this endpoint repeatedly while they wait.
    Each fetch returns one cycle of the hold experience (position announcement
    + a chunk of hold media); SignalWire calls us again when that cycle
    finishes, so position updates naturally reflect the caller advancing.

    SignalWire passes the call's queue context in the request body. We read
    `entry_position` (1-indexed) and `wait_time` (seconds elapsed) when present
    and weave them into the announcement.

    Endpoint is webhook-auth gated via the signed URL — the wait_url emitted
    in bridge.build_ingress_swml is signed by signed_webhook_url so we know
    the request actually came from SignalWire and not from a stray client.
    """
    # Parse position + wait time from SignalWire's request body. Field names
    # come from the enter_queue output schema (entry_position, wait_time).
    data = request.get_json(silent=True) if request.is_json else None
    data = data or request.form.to_dict() or {}
    position = data.get('entry_position') or data.get('position') or 0
    waited = data.get('wait_time') or data.get('waited_seconds') or 0
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = 0
    try:
        waited = int(waited)
    except (TypeError, ValueError):
        waited = 0

    logger.info(
        f"queue-wait fetch: queue={queue_slug} position={position} waited={waited}s"
    )

    # Compose the announcement. Position 1 = "you're next." Higher = "you're #N."
    # First fetch (position 0, no data yet from SignalWire) = generic welcome.
    if position == 1:
        announcement = (
            "say:You're next in line. The next available agent will be with you shortly."
        )
    elif position > 1:
        announcement = (
            f"say:You are number {position} in queue. "
            f"Please continue to hold and we'll connect you with the next available agent."
        )
    else:
        announcement = (
            "say:Thanks for holding. We'll connect you with the next available agent shortly."
        )

    # One announcement + a chunk of silence. SignalWire calls us again once
    # the silence finishes, giving us a chance to re-announce position.
    #
    # Format note: use the STRING SHORTHAND `{"play": "say:..."}` here, NOT
    # the object form `{"play": {"url": "..."}}`. The play schema docs list
    # `url` (singular) as an alias for `urls` (plural) but every working
    # example uses either string shorthand or `urls` array — `url` singular
    # appears to silently fail-parse: verb completes in ~0ms, SignalWire
    # re-fetches wait_url immediately. That manifests as a tight loop of
    # `/queue-wait/<slug>` hits in ngrok and the caller hearing nothing.
    return jsonify({
        "version": "1.0.0",
        "sections": {
            "main": [
                {"play": announcement},
                # 25 seconds between announcements feels right — frequent
                # enough to update position, infrequent enough not to be
                # annoying.
                {"play": "silence:25"},
            ]
        }
    })


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
                                "webhook": signed_webhook_url(f"{base_url}/api/webhooks/transcription"),
                                "lang": "en-US",
                                "live_events": True,
                                "partial_events": False,
                                "direction": ["remote-caller"],
                                "beep": True,
                                "timeout": 30,
                                # Raise VAD silence from the 300ms default so
                                # mid-sentence pauses don't split one utterance
                                # into multiple transcript lines (see initial-call).
                                "vad_silence_ms": 800,
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
                    # PLAT-01 fix (2026-06-02 audit): TranscribeAction's stop
                    # variant is a bare-string const per the SWML schema —
                    # {action: "stop"}, not {action: {stop: {}}}. The object
                    # form is non-conformant and silently parse-fails on
                    # SignalWire's side, so live_transcribe never actually
                    # stops and keeps streaming/billing. The REST helper
                    # (signalwire_api.stop_transcription) already uses the
                    # correct bare-string form — this is the SWML side
                    # catching up.
                    "live_transcribe": {
                        "action": "stop"
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
                                "webhook": signed_webhook_url(f"{base_url}/api/webhooks/summary")
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


@swml_bp.route('/ai-agent-proxy', methods=['POST'])
def ai_agent_proxy():
    """Proxy AI agent SWML requests from SignalWire.

    This endpoint solves the auth problem: the AI agents container requires
    Basic Auth, but SignalWire doesn't include auth when fetching 'url' param.
    This proxy fetches internally (with auth) and returns the SWML to SignalWire.

    The AI agent uses request headers to generate SWAIG callback URLs, so we
    forward the original Host/X-Forwarded headers from the SignalWire request.
    """
    agent_route = request.args.get('agent', '/receptionist')
    conf = request.args.get('conf', '')
    call_db_id = request.args.get('call_db_id', '')

    # ISO-10 (2026-07-07 pre-deploy): this proxy attaches internal Basic
    # creds and forwards to a CLIENT-SUPPLIED destination. Left open,
    # ?agent=@evil-host/x credential-leaks to an attacker's host and
    # ?agent=/<anything> reaches any ai-agents route. Restrict `agent` to a
    # fixed allowlist of the real agent routes — no host component, exact
    # match only. (This endpoint is public via nginx `location /api`.)
    _ALLOWED_AGENT_ROUTES = {
        '/receptionist', '/sales', '/support', '/sales-ai', '/support-ai',
        '/outbound-sales', '/outbound-support', '/hold-ai',
    }
    if agent_route not in _ALLOWED_AGENT_ROUTES:
        logger.warning("[AI-PROXY] rejected disallowed agent route: %r", agent_route)
        return jsonify({'error': 'Unknown agent route'}), 400

    # Build the internal URL to the AI agent
    query_parts = []
    if conf:
        query_parts.append(f"conf={conf}")
    if call_db_id:
        query_parts.append(f"call_db_id={call_db_id}")
    query_string = '&'.join(query_parts)

    internal_url = f"http://ai-agents:8080{agent_route}"
    if query_string:
        internal_url += f"?{query_string}"

    try:
        # Forward the request with auth and original headers
        agent_user = os.environ.get('AGENT_AUTH_USER', 'agent')
        agent_pass = os.environ.get('AGENT_AUTH_PASS', 'agent123')

        # Forward relevant headers so AI agent generates correct external URLs
        forward_headers = {
            'Content-Type': 'application/json',
            'Host': request.headers.get('Host', ''),
            'X-Forwarded-Proto': request.headers.get('X-Forwarded-Proto', 'https'),
            'X-Forwarded-Host': request.headers.get('X-Forwarded-Host', request.headers.get('Host', '')),
            'X-Real-IP': request.headers.get('X-Real-IP', request.remote_addr),
        }

        resp = http_requests.post(
            internal_url,
            json=request.get_json(silent=True) or {},
            auth=(agent_user, agent_pass),
            headers=forward_headers,
            timeout=10
        )

        logger.warning(f"[AI-PROXY] Fetched AI SWML from {internal_url}: {resp.status_code}")

        if resp.status_code == 200:
            return jsonify(resp.json())
        else:
            logger.error(f"[AI-PROXY] AI agent returned {resp.status_code}: {resp.text[:200]}")
            return jsonify({"error": "Failed to fetch AI SWML"}), 502

    except Exception as e:
        logger.error(f"[AI-PROXY] Error proxying to AI agent: {str(e)}")
        return jsonify({"error": str(e)}), 500
