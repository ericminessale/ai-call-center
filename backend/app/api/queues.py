"""
Queue Management API Endpoints
Handles call queuing, agent assignment, and queue monitoring
"""

from flask import Blueprint, jsonify, request, current_app
from app.services.queue_service import QueueService
from app.services.redis_service import get_redis_client
from app.services.callcenter_socketio import emit_call_update
from app.utils.decorators import require_auth
from app.utils.demo_config import block_in_demo_mode, is_demo_mode
from app.utils.url_utils import get_base_url, signed_webhook_url
from app.utils.webhook_auth import require_webhook_auth
from app import db
from app.models import Call, User, Conference, ConferenceParticipant, CallLeg, Contact
from app.models.queue import Queue, QueueAgentAssignment
from datetime import datetime
import logging
import json
import os
import base64

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
@require_webhook_auth
def route_call_to_queue(queue_id):
    """
    Route an incoming call to a queue
    Called by AI agents via SWML transfer
    Returns SWML to place caller on hold while waiting for agent

    Auth: HTTP Basic. SignalWire calls this URL after the AI agent's
    transfer SWML; the agent embeds WEBHOOK_AUTH_USER:WEBHOOK_AUTH_PASSWORD
    in the URL it returns. See app.utils.webhook_auth.
    """
    try:
        logger.info(f"Queue route hit: /api/queues/{queue_id}/route")
        data = request.json or {}
        logger.info(f"Queue route received data: {json.dumps(data, default=str)[:1000]}")

        # Extract call information from SignalWire webhook
        # SignalWire sends call info nested under 'call' key
        call_data = data.get('call', {})
        call_id = call_data.get('call_id') or data.get('CallSid') or data.get('call_id')
        caller_number = call_data.get('from_number') or data.get('From') or data.get('caller_number')

        # PRIORITY 1: Check for base64-encoded context in URL query param (most reliable)
        # AI agents encode context as ?ctx=<base64> in the transfer URL
        ctx_param = request.args.get('ctx')
        url_context = {}
        if ctx_param:
            try:
                ctx_json = base64.urlsafe_b64decode(ctx_param.encode()).decode()
                url_context = json.loads(ctx_json)
                logger.info(f"Decoded URL context: {json.dumps(url_context)}")
            except Exception as e:
                logger.warning(f"Failed to decode ctx param: {e}")

        # PRIORITY 2: Get context from request body global_data (backup)
        # The AI agents also set global_data which SignalWire may or may not forward
        global_data = data.get('global_data', {})

        # Merge: URL context takes priority over body global_data
        # This ensures we get the data even if SignalWire doesn't forward global_data
        merged_global_data = {**global_data, **url_context}
        global_data = merged_global_data

        logger.info(f"Merged context data: {json.dumps(global_data, default=str)}")
        logger.debug(f"caller_number: {caller_number}, call_id: {call_id}")

        context = {
            # Direct fields (legacy support)
            'customer_name': data.get('customer_name') or global_data.get('customer_name'),
            'account_number': data.get('account_number') or global_data.get('account_number'),
            'issue_description': data.get('issue_description') or global_data.get('issue') or global_data.get('reason'),
            'priority': data.get('priority') or global_data.get('priority', 5),
            'ai_summary': data.get('ai_summary') or global_data.get('ai_summary'),
            # Fields from AI agents
            'reason': global_data.get('reason'),
            'issue': global_data.get('issue'),
            'urgency': global_data.get('urgency'),
            'department': global_data.get('department'),
            'interest': global_data.get('interest'),
            'company': global_data.get('company'),
            'budget': global_data.get('budget'),
            'error_message': global_data.get('error_message'),
            'source_agent': global_data.get('source_agent'),
            'caller_language': global_data.get('caller_language'),
            # Keep full global_data as fallback
            'global_data': global_data
        }

        # Caller's preferred language (BCP-47, e.g. 'es-ES'). Used by select_agent
        # to prefer language-matched agents and decide whether translation is needed.
        caller_language = global_data.get('caller_language') or 'en-US'

        # Clean up None values
        context = {k: v for k, v in context.items() if v is not None}

        # If no meaningful context was provided (direct inbound, no AI triage),
        # mark it so the agent dashboard shows something useful
        if not context or context == {'global_data': {}} or context.get('global_data') == {}:
            context['source'] = context.get('source', 'direct_inbound')

        # Map urgency to priority if urgency is set but priority isn't
        urgency = context.get('urgency', '').lower()
        if urgency and context.get('priority', 5) == 5:  # Only if priority is default
            urgency_map = {'high': 2, 'medium': 5, 'low': 8}
            context['priority'] = urgency_map.get(urgency, 5)

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
                status='waiting',  # Start as 'waiting' in queue
                destination_type='phone',
                handler_type='human',
                created_at=datetime.utcnow(),
                queue_id=queue_id  # Track which queue they're in
            )
            db.session.add(call)

        # Store AI context (customer info collected by AI agent)
        call.ai_context = json.dumps(context) if context else None

        # Persist caller's language so the agent UI + auto-start hooks can read it
        call.caller_language = caller_language

        # Ensure call is marked as 'waiting' in queue
        if call.status not in ['waiting', 'assigned', 'active', 'ended']:
            call.status = 'waiting'
        call.queue_id = queue_id

        # Update Contact record with AI-collected information
        contact_id = None
        if caller_number:
            try:
                contact = Contact.find_or_create_by_phone(caller_number)
                contact_id = contact.id
                contact_updated = False

                # Parse customer_name into first/last name
                customer_name = context.get('customer_name')
                if customer_name:
                    # Update display_name if not set OR if it's just a phone number
                    current_display = contact.display_name or ''
                    is_phone_display = current_display.startswith('+') or current_display.isdigit()
                    if not contact.display_name or is_phone_display:
                        contact.display_name = customer_name
                        contact_updated = True
                        logger.info(f"Updated contact display_name to: {customer_name}")

                    # Try to parse into first/last name if not already set OR if display was phone
                    if not contact.first_name or is_phone_display:
                        name_parts = customer_name.strip().split(' ', 1)
                        if len(name_parts) >= 1:
                            contact.first_name = name_parts[0]
                            contact_updated = True
                            logger.info(f"Updated contact first_name to: {name_parts[0]}")
                        if len(name_parts) >= 2:
                            contact.last_name = name_parts[1]
                            contact_updated = True
                            logger.info(f"Updated contact last_name to: {name_parts[1]}")

                # Update company if AI collected it and contact doesn't have one
                company = context.get('company')
                if company and not contact.company:
                    contact.company = company
                    contact_updated = True

                # Update last interaction timestamp
                contact.last_interaction_at = datetime.utcnow()
                contact.total_calls = (contact.total_calls or 0) + 1
                contact_updated = True

                # Store additional AI context in custom_fields
                extra_fields = {}
                for field in ['department', 'interest', 'budget', 'urgency']:
                    if context.get(field):
                        extra_fields[field] = context[field]

                if extra_fields:
                    existing_custom = contact.custom_fields_dict or {}
                    existing_custom.update(extra_fields)
                    contact.custom_fields_dict = existing_custom
                    contact_updated = True

                # Link call to contact
                if call:
                    call.contact_id = contact.id

                if contact_updated:
                    logger.info(f"Updated contact {contact.id} ({contact.phone}) with AI-collected data")
                    # Emit contact update via WebSocket so frontend can refresh
                    from app import socketio
                    socketio.emit('contact_update', {
                        'contact': contact.to_dict_minimal()
                    })
                    logger.info(f"Emitted contact_update for contact {contact.id}")

            except Exception as e:
                logger.error(f"Error updating contact with AI data: {str(e)}")
                # Don't fail the queue routing if contact update fails

        db.session.commit()

        # Compute strategy params for the unified helper. /route gets richer
        # data than /direct-inbound (skill levels for skill_based/priority
        # routing, language-matched dispatch) — pass them through.
        service = get_queue_service()
        available_agents = service.get_available_agents(queue_id)
        queue_config = Queue.find_by_slug(queue_id)
        routing_strategy = queue_config.routing_strategy if queue_config else 'round_robin'

        skill_levels = {}
        if routing_strategy in ('skill_based', 'priority') and available_agents:
            skill_levels = service.get_skill_levels_for_queue(queue_id, available_agents)

        agent_languages = {}
        if available_agents:
            agent_languages = service.get_languages_for_agents(available_agents)

        # Unified queue onboarding — same helper used by /direct-inbound. From
        # here both ingress paths (PSTN direct + AI-agent transfer) follow the
        # same lifecycle: enqueue in Redis, emit queue_update, immediate-
        # dispatch if an agent is available, start announcement loop otherwise,
        # return SWML. Transport choice (conference vs bridge) is hidden
        # behind call_transport.build_ingress_swml. Today M0 always picks
        # conference; M1 lets admins opt queues into bridge mode.
        from app.services.call_transport import build_ingress_swml
        base_url = get_base_url()
        swml_response = build_ingress_swml(
            call=call,
            queue_slug=queue_id,
            context=context,
            base_url=base_url,
            routing_strategy=routing_strategy,
            caller_language=caller_language,
            agent_languages=agent_languages,
            skill_levels=skill_levels,
            priority=priority,
            # AI agents transfer here mid-conversation. live_transcribe may
            # already be running on the caller leg from the agent's SWML; re-
            # starting is harmless (no-op on SignalWire's side) and ensures
            # the post-transfer transcripts continue flowing to our webhook
            # for the /direct-inbound case where nothing started it yet.
            start_live_transcribe=True,
        )
        logger.info(f"/route call {call_id} → transport={call.transport} (conference name: interaction-{call_id})")
        return jsonify(swml_response)


    except Exception as e:
        logger.error(f"Error routing call to queue {queue_id}: {str(e)}")
        # Critical cleanup: the exception happened AFTER enqueue_call emitted
        # queue_update 'added' to the frontend. SignalWire is about to hang up
        # the caller (per our 500-response SWML below) but no call_status webhook
        # may fire to drive the normal cleanup. Without these emits, the call
        # sits forever in the frontend's queuedCalls list as a ghost banner.
        try:
            call_to_end = Call.find_by_sid(call_id) if call_id else None
            if call_to_end:
                call_to_end.update_status('failed')
                call_to_end.ended_at = call_to_end.ended_at or datetime.utcnow()
                db.session.commit()
                from app import socketio
                socketio.emit('call_ended', {
                    'callId': call_to_end.id,
                    'call_sid': call_to_end.signalwire_call_sid,
                    'reset_ui': True,
                })
                socketio.emit('queue_update', {
                    'call': call_to_end.to_dict(include_contact=True),
                    'queue_id': queue_id,
                    'action': 'ended',
                })
                try:
                    if get_redis_client():
                        get_queue_service().remove_call_from_all_queues(call_id)
                except Exception as cleanup_err:
                    logger.warning(f"Redis queue cleanup failed: {cleanup_err}")
                logger.info(f"Emitted cleanup events for failed-route call {call_id}")
        except Exception as cleanup_err:
            logger.error(f"Cleanup after /route failure also failed: {cleanup_err}")
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


@queues_bp.route('/<queue_slug>/direct-inbound', methods=['POST'])
def direct_inbound_queue(queue_slug):
    """Direct inbound call entry point — bypasses AI, routes straight to human queue.

    Caller-in-conference architecture (as of 2026-05-13):
    - Caller is placed in ``interaction-<call_sid>`` conference immediately
    - If an agent is available at call arrival, they're notified now; their
      WebRTC leg joins the SAME conference on Accept
    - If no agent is available, caller stays in the conference with hold
      media. When an agent later flips to 'available', queue_service's
      push-dispatch hook fires the SAME notification path — no /route polling
      needed.

    Phase 2 (separate task): periodic per-caller TTS announcements via
    `play_tts` on the participant + DTMF collection for IVR (callback / AI
    specialist / continue holding).
    """
    try:
        logger.info(f"Direct inbound call → queue '{queue_slug}'")
        data = request.json or {}
        call_data = data.get('call', {})
        call_id = call_data.get('call_id') or data.get('CallSid')
        caller_number = call_data.get('from_number') or data.get('From')
        to_number = call_data.get('to_number') or data.get('To')

        logger.info(f"Direct inbound: call_id={call_id}, from={caller_number}, to={to_number}")

        queue = Queue.query.filter_by(slug=queue_slug).first()
        if not queue:
            logger.warning(f"Direct inbound: unknown queue '{queue_slug}'")
            return jsonify({
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {"play": {"url": "say:We're sorry, this number is not configured correctly. Please try again later."}},
                        "hangup"
                    ]
                }
            })

        # Caller's conference — same name used by push-dispatch when an agent
        # later flips to available. Deterministic from call_sid so any code
        # path can derive it without DB lookup.
        conference_name = f"interaction-{call_id}"

        # Create / hydrate Call record so webhooks + dashboard can track it
        call = Call.query.filter_by(signalwire_call_sid=call_id).first() if call_id else None
        if not call:
            system_user = User.query.filter_by(email='system@signalwire.local').first()
            if not system_user:
                system_user = db.session.query(User).first()

            # Demo phone-verification attribution (see initial-call): a call
            # from a verified number belongs to the persona that verified it,
            # making it private to that visitor via the ownership checks.
            owner_user_id = system_user.id
            if is_demo_mode() and caller_number:
                try:
                    from app.services.demo_verify import get_persona_for_number
                    persona_id = get_persona_for_number(caller_number)
                    if persona_id:
                        owner_user_id = persona_id
                except Exception as exc:
                    logger.warning("direct_inbound: verify attribution failed (non-fatal): %s", exc)

            call = Call(
                signalwire_call_sid=call_id,
                user_id=owner_user_id,
                from_number=caller_number,
                destination=to_number or 'unknown',
                destination_type='phone',
                direction='inbound',
                handler_type='human',
                # NOTE: 'waiting' (not 'pending') because we can't get
                # call-state webhooks without configuring the phone
                # number's call_status_callback_url at the SignalWire-
                # side (TODO: do that and revert to 'pending' + promote
                # on 'created'/'answered'). Without that, 'pending' rows
                # would never get promoted and the call would never show
                # in the queue UI. Carrier auto-retry storms (one failed
                # dial → 8 webhooks in 19s) still create phantom rows
                # here — needs a watchdog cleanup sweep.
                status='waiting',
                queue_id=queue_slug,
                ai_context=json.dumps({'source': 'direct_inbound', 'queue': queue_slug}),
                transcription_active=True,
                conference_name=conference_name,
                created_at=datetime.utcnow()
            )
            db.session.add(call)
        else:
            call.conference_name = conference_name

        # Contact lookup / create
        contact_id = None
        if caller_number:
            try:
                contact = Contact.find_or_create_by_phone(caller_number)
                call.contact_id = contact.id
                contact_id = contact.id
                contact.last_interaction_at = datetime.utcnow()
                contact.total_calls = (contact.total_calls or 0) + 1
            except Exception as e:
                logger.warning(f"Direct inbound: failed to create contact: {e}")

        db.session.commit()

        # NOTE: deliberately NOT emitting call_update / queue_update here.
        # Call is in 'pending' status until SignalWire confirms the leg
        # established (call-state 'created'/'answered' on /api/webhooks/call-status)
        # OR the caller is actually parked in SignalWire's queue
        # (status='entering' on /api/webhooks/queue-status). Whichever
        # webhook fires first promotes 'pending' -> 'waiting' and emits
        # both queue_update {action: 'added'} and call_update. Carrier
        # auto-retry storms (8 PSTN hits in 19s from a failed dial) used
        # to spam the queue UI here; now phantom dials stay invisible until
        # confirmed.

        # Unified queue onboarding — call_transport.build_ingress_swml
        # dispatches on the queue's routing_transport (conference today;
        # M1 lets admins opt into bridge). Handles Conference DB row,
        # Redis enqueue, queue_update emit, immediate dispatch + agent
        # notification, announcement loop, and SWML build.
        from app.services.call_transport import build_ingress_swml
        swml_response = build_ingress_swml(
            call=call,
            queue_slug=queue_slug,
            context={
                'source': 'direct_inbound',
                'queue': queue_slug,
                'priority': 5,
                'contact_id': contact_id,
            },
            base_url=get_base_url(),
            routing_strategy=queue.routing_strategy if queue else 'round_robin',
            caller_language='en-US',
            priority=5,
            start_live_transcribe=True,
        )

        logger.info(
            f"Direct inbound call {call_id} → transport={call.transport} "
            f"(conference name: {conference_name})"
        )
        return jsonify(swml_response)

    except Exception as e:
        logger.error(f"Error in direct_inbound_queue: {str(e)}")
        return jsonify({
            "version": "1.0.0",
            "sections": {
                "main": [
                    {"play": {"url": "say:We're experiencing technical difficulties. Please try again later."}},
                    "hangup"
                ]
            }
        }), 500


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


@queues_bp.route('/wallboard', methods=['GET'])
@require_auth
def get_wallboard():
    """Aggregate wallboard row per active queue (IMP-18).

    One call instead of N per-queue /status fetches: live depth/waits from
    Redis, 24h service level against each queue's own SLA threshold, and
    24h offered/answered/abandoned counts from the Call table (end_reason
    'abandoned_in_queue' is stamped deterministically at call end).
    """
    try:
        from datetime import timedelta
        from sqlalchemy import func, case

        service = get_queue_service()
        since = datetime.utcnow() - timedelta(hours=24)

        rows = []
        for queue in Queue.get_active_queues():
            metrics = service.get_queue_metrics(queue.slug)
            metrics.pop('calls', None)  # wallboard doesn't need per-call previews

            counts = db.session.query(
                func.count(Call.id),
                func.sum(case((Call.answered_at.isnot(None), 1), else_=0)),
                func.sum(case((Call.end_reason == 'abandoned_in_queue', 1), else_=0)),
            ).filter(
                Call.queue_id == queue.slug,
                Call.created_at >= since,
            ).one()
            offered = int(counts[0] or 0)
            answered = int(counts[1] or 0)
            abandoned = int(counts[2] or 0)

            rows.append({
                **metrics,
                'slug': queue.slug,
                'display_name': queue.display_name,
                'offered_24h': offered,
                'answered_24h': answered,
                'abandoned_24h': abandoned,
                'abandon_rate': round(100.0 * abandoned / offered, 1) if offered else None,
            })

        return jsonify({'queues': rows, 'window_hours': 24})

    except Exception as e:
        logger.error(f"Error building wallboard: {str(e)}")
        return jsonify({"error": "Failed to build wallboard"}), 500


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
            for queue_id in Queue.get_active_slugs():
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
        return jsonify(metrics)

    except Exception as e:
        logger.error(f"Error getting agent metrics: {str(e)}")
        return jsonify({"error": "Failed to get metrics"}), 500


@queues_bp.route('/transfer', methods=['POST'])
@require_auth
def transfer_call():
    """Transfer a call to another agent or queue — NOT YET IMPLEMENTED.

    LIFE-02 (2026-06-02 audit). The previous implementation wrote a
    transfer_history row + emitted ``call_transferred`` Socket.IO but
    NEVER actually moved the participant on SignalWire's side. The
    audio bridge stayed unchanged, the DB lied to the rest of the
    stack, and any consumer reading transfer_history saw a transfer
    that didn't happen.

    No frontend currently calls this — the previous implementation was
    a latent footgun. Returning 501 until the real path is wired
    through ``conferences.move_participant`` (which actually relocates
    a participant between conferences on the SignalWire side). Pair
    with a design pass on warm-vs-blind semantics + target validation
    + announcement TTS before re-enabling.

    Sibling Socket.IO handler ``transfer_call`` in
    ``callcenter_socketio.py`` was disabled identically.
    """
    return jsonify({
        'error': 'Transfer not implemented',
        'detail': (
            'The previous implementation desynced state vs. the SignalWire '
            'call object — see LIFE-02 in REMEDIATION_2026-06-02.md. Use '
            '/api/call-control/<id>/return-to-queue or '
            '/api/call-control/<id>/request-backup until a proper transfer '
            'path lands.'
        ),
    }), 501


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
        queue_ids = Queue.get_active_slugs()

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


@queues_bp.route('/all/calls', methods=['GET'])
@require_auth
def get_all_queued_calls():
    """
    Get all calls currently in queue (waiting, assigned, or urgent)
    Returns calls sorted by urgency (urgent first, then waiting, then assigned)
    """
    try:
        # Query calls that are in queue states
        # Status can be: waiting, assigned
        # urgent is computed dynamically via the is_urgent property
        queued_calls = Call.query.filter(
            Call.status.in_(['waiting', 'assigned'])
        ).order_by(Call.created_at.asc()).all()

        # Convert to dicts and sort by urgency
        calls_data = []
        for call in queued_calls:
            call_dict = call.to_dict(include_contact=True)
            calls_data.append(call_dict)

        # Sort by urgency: urgent first, then by wait time
        # queue_status will be 'urgent', 'waiting', or 'assigned'
        urgency_order = {'urgent': 0, 'waiting': 1, 'assigned': 2}
        calls_data.sort(key=lambda c: (
            urgency_order.get(c.get('queue_status', 'assigned'), 3),
            -c.get('wait_time_seconds', 0)  # Longer wait = higher priority
        ))

        logger.info(f"Returning {len(calls_data)} queued calls")

        return jsonify({
            'calls': calls_data,
            'total': len(calls_data)
        })

    except Exception as e:
        logger.error(f"Error getting queued calls: {str(e)}")
        return jsonify({"error": "Failed to get queued calls"}), 500


@queues_bp.route('/mock/clear', methods=['POST'])
@require_auth
@block_in_demo_mode
def clear_mock_data():
    """
    Clear all mock/demo calls from queues

    Blocked in hosted-demo mode: any leased persona could otherwise wipe
    queue state out from under every other visitor.
    """
    try:
        service = QueueService()
        cleared_count = 0

        # Clear demo calls from all queues
        for queue_id in Queue.get_active_slugs():
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
@block_in_demo_mode
def generate_mock_data():
    """
    Generate mock queue data for demos

    Blocked in hosted-demo mode: visitors get real calls, not mock rows,
    and unbounded mock generation is a state-spam vector.
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
        for queue_id in Queue.get_active_slugs():
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


# =============================================================================
# Agent-facing queue endpoints
# =============================================================================

@queues_bp.route('/my-queues', methods=['GET'])
@require_auth
def get_my_queues():
    """Get the current agent's assigned queues with activation status."""
    try:
        user_id = request.current_user.id
        assignments = QueueAgentAssignment.query.filter_by(user_id=user_id).all()
        return jsonify({
            'queues': [a.to_dict() for a in assignments]
        }), 200
    except Exception as e:
        logger.error(f"Failed to get agent queues: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/my-queues/<int:assignment_id>/activate', methods=['PUT'])
@require_auth
def toggle_queue_activation(assignment_id):
    """Agent toggles their activation for a queue."""
    try:
        user_id = request.current_user.id
        assignment = QueueAgentAssignment.query.filter_by(
            id=assignment_id, user_id=user_id
        ).first()
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404

        data = request.get_json() or {}
        assignment.is_activated = data.get('is_activated', not assignment.is_activated)
        db.session.commit()

        # Update Redis agent-queue membership for real-time routing
        redis_client = get_redis_client()
        if redis_client:
            queue_agents_key = f"queue_agents:{assignment.queue.slug}"
            if assignment.is_activated:
                redis_client.sadd(queue_agents_key, str(user_id))
            else:
                redis_client.srem(queue_agents_key, str(user_id))

        # Broadcast activation change
        from app import socketio
        socketio.emit('agent_queue_activation', {
            'user_id': user_id,
            'queue_slug': assignment.queue.slug,
            'is_activated': assignment.is_activated,
        })

        return jsonify({'success': True, 'assignment': assignment.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to toggle queue activation: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/available', methods=['GET'])
@require_auth
def get_available_queues():
    """Returns all active queues annotated with the calling agent's assignment state.
    Used by the status dropdown for queue opt-in."""
    try:
        user_id = request.current_user.id
        queues = Queue.get_active_queues()

        # Build lookup of existing assignments for this user
        assignments = QueueAgentAssignment.query.filter_by(user_id=user_id).all()
        assignment_map = {a.queue_id: a for a in assignments}

        result = []
        for q in queues:
            data = q.to_dict()
            a = assignment_map.get(q.id)
            data['assignment_id'] = a.id if a else None
            data['is_assigned'] = a is not None
            data['is_activated'] = a.is_activated if a else False
            data['skill_level'] = a.skill_level if a else 5
            result.append(data)

        return jsonify({'queues': result}), 200
    except Exception as e:
        logger.error(f"Failed to get available queues: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/self-subscribe/<int:queue_id>', methods=['POST'])
@require_auth
def self_subscribe_queue(queue_id):
    """Agent self-subscribes to a queue. Creates assignment if none exists, activates it."""
    try:
        user_id = request.current_user.id
        queue = Queue.query.get(queue_id)
        if not queue:
            return jsonify({'error': 'Queue not found'}), 404
        if not queue.is_active:
            return jsonify({'error': 'Queue is not active'}), 400

        # Find or create assignment
        assignment = QueueAgentAssignment.query.filter_by(
            queue_id=queue_id, user_id=user_id
        ).first()

        if assignment:
            assignment.is_activated = True
        else:
            assignment = QueueAgentAssignment(
                queue_id=queue_id,
                user_id=user_id,
                skill_level=5,
                is_activated=True,
            )
            db.session.add(assignment)

        db.session.commit()

        # Update Redis
        redis_client = get_redis_client()
        if redis_client:
            redis_client.sadd(f"queue_agents:{queue.slug}", str(user_id))

        # Broadcast
        from app import socketio
        socketio.emit('agent_queue_activation', {
            'user_id': user_id,
            'queue_slug': queue.slug,
            'is_activated': True,
        })

        return jsonify({'success': True, 'assignment': assignment.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to self-subscribe to queue: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/config/active', methods=['GET'])
def get_active_queue_config():
    """Public endpoint: returns the default workspace's active queues.
    Used by AI agents at startup to build dynamic triage contexts.
    No auth required — internal use only.

    Tenancy: this endpoint is unauthenticated, so the workspace auto-filter
    is off — an unqualified query would return every visitor workspace's
    queue clones too, and the duplicate slugs crash the ai-agents boot
    (ContextBuilder raises on a repeated context name). Pin it to the
    default/template workspace: in clone-and-own that is ALL queues
    (identical to pre-tenancy), in hosted mode it is the template set the
    shared agent process serves until Phase 4's per-request resolution.
    """
    try:
        from app.tenancy import DEFAULT_WORKSPACE_ID
        queues = (
            Queue.query
            .filter_by(is_active=True, workspace_id=DEFAULT_WORKSPACE_ID)
            .order_by(Queue.display_name)
            .all()
        )
        return jsonify({
            'queues': [q.to_dict() for q in queues]
        }), 200
    except Exception as e:
        logger.error(f"Failed to get active queue config: {str(e)}")
        return jsonify({'error': str(e)}), 500