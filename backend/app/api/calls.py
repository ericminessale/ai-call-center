from flask import request, jsonify
from app import db, socketio, redis_client
from app.api import calls_bp
from app.models import Call, CallLeg, Transcription
from app.services.signalwire_api import get_signalwire_api
from app.utils.decorators import require_auth, require_permission, require_role, validate_json
from app.utils.demo_config import is_demo_mode, block_in_demo_mode, DEMO_BLOCKED_RESPONSE
from app.utils.rate_limit import rate_limit
from app.utils.feature_flags import require_coach_enabled
from app.utils.webhook_auth import require_internal_auth
from app.utils.moderation import is_text_acceptable
from app.utils.url_utils import get_base_url, signed_webhook_url
from app.services.queue_service import QueueService
from app.services import cost_service
from datetime import datetime, timedelta
from sqlalchemy import func
import logging
import secrets
import json
import os

logger = logging.getLogger(__name__)


def _require_call_ownership(call, user, allow_demo_ai_view=False):
    """Return an (jsonify, status) abort tuple, or None to allow.

    ISO-2/ISO-4 (2026-07-07 pre-deploy): the /api/calls/<id>/* routes gate
    only on @require_auth with an enumerable integer id, so any authenticated
    user (including a leased demo persona) could read or mutate any other
    visitor's call by guessing the id. Reject unless the requester owns the
    call (initiated it or is the assigned agent) OR holds supervisor/admin.

    Mirrors call_control._require_call_ownership so the two call-control
    surfaces share one authorization model.

    ``allow_demo_ai_view``: retained for signature compatibility; the
    shared-floor read allowance it used to enable is gone with the shared
    floor (hosted visitors are admins of their own workspace and pass the
    role check; tenancy auto-filtering stops cross-workspace lookups).
    """
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    role = getattr(user, 'role', '') or ''
    if role in ('admin', 'supervisor'):
        return None
    if call.assigned_agent_id == user.id or call.user_id == user.id:
        return None
    # (The old shared-floor read allowance for demo personas is gone with
    # the shared floor itself — hosted visitors are admins of their own
    # workspace and pass the role check above; the tenancy auto-filter
    # already stops cross-workspace call lookups from resolving at all.)
    return jsonify({
        'error': "You don't have access to this call",
        'detail': (
            'Only the call owner or assigned agent (or a supervisor/admin) '
            'can act on this call.'
        ),
    }), 403


@calls_bp.route('/cost-rates', methods=['GET'])
@require_auth
def get_cost_rates():
    """Published list rates the cost estimator uses (SystemConfig pricing.*)."""
    return jsonify({
        'rates': cost_service.get_rates(),
        'disclaimer': 'Estimated at published list rates',
    })


@calls_bp.route('/cost-summary', methods=['GET'])
@require_auth
def get_cost_summary():
    """Today's estimated platform spend across all calls (IMP-01).

    Re-runs the same per-call estimator the call rows embed, so the day
    total always agrees with the line items. Demo-scale query (one day of
    calls); switch to a SQL aggregate if volumes ever matter.
    """
    try:
        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        calls = Call.query.filter(
            Call.created_at >= since,
            Call.duration.isnot(None),
            Call.duration > 0,
        ).all()

        total = 0.0
        ai_minutes = 0.0
        human_minutes = 0.0
        for call in calls:
            estimate = cost_service.estimate_call_cost(call)
            if not estimate:
                continue
            total += estimate['total']
            if call.handler_type == 'ai':
                ai_minutes += estimate['minutes']
            else:
                human_minutes += estimate['minutes']

        rates = cost_service.get_rates()
        return jsonify({
            'since': since.isoformat(),
            'call_count': len(calls),
            'total_estimated': round(total, 2),
            'ai_minutes': round(ai_minutes, 1),
            'human_minutes': round(human_minutes, 1),
            'did_monthly': rates['did_monthly'],
            'rates': rates,
            'disclaimer': 'Estimated at published list rates',
        })
    except Exception as e:
        logger.error(f"Failed to build cost summary: {str(e)}")
        return jsonify({'error': 'Failed to build cost summary'}), 500


@calls_bp.route('/<call_sid>/cost', methods=['GET'])
@require_auth
def get_call_cost(call_sid):
    """Line-item cost estimate for one call."""
    call = Call.find_by_sid(call_sid)
    if not call and call_sid.isdigit():
        call = db.session.get(Call, int(call_sid))
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    estimate = cost_service.estimate_call_cost(call)
    if not estimate:
        return jsonify({'error': 'No billable duration on this call yet'}), 404
    return jsonify({
        'call_sid': call.signalwire_call_sid,
        'estimate': estimate,
    })


@calls_bp.route('/initiate', methods=['POST'])
@require_auth
@validate_json('destination', 'destination_type')
def initiate_call():
    """Initiate a new outbound call.

    In DEMO_MODE this is gated to "your own verified number only" (phone
    verification): a persona may dial the number it verified via the pairing
    flow, nothing else. Unverified visitors / non-phone destinations get 403
    'demo_verify_required' / 'demo_blocked' so the UI can prompt to verify.
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

        # Demo outbound gate: phone-to-own-verified-number only. SIP has no
        # number to verify against, so it stays blocked in demo.
        if is_demo_mode():
            if destination_type != 'phone':
                return jsonify(DEMO_BLOCKED_RESPONSE), 403
            from app.services.demo_verify import demo_outbound_denial
            denial = demo_outbound_denial(request.current_user.workspace_id, destination)
            if denial:
                return jsonify(denial[0]), denial[1]

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

        # Emit call initiated event. Rooms are joined as str(user_id) —
        # an int here silently targets a room with no members.
        socketio.emit('call_initiated', {
            'call_sid': call_id,  # Frontend expects call_sid but we send call_id
            'destination': destination,
            'user_id': request.current_user.id
        }, room=str(request.current_user.id))

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

        # ISO-4: transcription control mutates call state — owner/privileged
        # only (no demo-view allowance for a mutation).
        owner_check = _require_call_ownership(call, request.current_user)
        if owner_check is not None:
            return owner_check

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

        # ISO-4: read gate — owner/privileged, or (demo mode) any persona
        # viewing an AI-handled call it's monitoring.
        owner_check = _require_call_ownership(
            call, request.current_user, allow_demo_ai_view=True
        )
        if owner_check is not None:
            return owner_check

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


@calls_bp.route('/<call_id>/kb-search', methods=['POST'])
@require_auth
@rate_limit('kb-search', 60, 60)
def kb_search(call_id):
    """KB Factbook: pgvector retrieval over a single collection.

    Body: {query: str, top_k?: int (1-20, default 5)}
    The collection is ALWAYS derived server-side from the call's queue →
    AI-agent → collection assignment (kb_collection_for_queue).
    """
    import requests as http_requests
    from app.services.knowledge import kb_collection_for_queue

    call = None
    if call_id.isdigit():
        call = db.session.query(Call).filter_by(id=int(call_id)).first()
    if not call:
        call = Call.find_by_sid(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    top_k = data.get('top_k', 5)
    collection_name = kb_collection_for_queue(call.queue_id, call.workspace_id)

    if not query:
        return jsonify({'error': 'query is required'}), 400
    if not isinstance(top_k, int) or top_k < 1 or top_k > 20:
        top_k = 5

    ai_agents_url = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')
    try:
        resp = http_requests.post(
            f"{ai_agents_url}/search",
            json={
                'collection_name': collection_name,
                'query': query,
                'top_k': top_k,
            },
            timeout=30,
        )
    except http_requests.RequestException as exc:
        logger.error(f"KB search proxy failed: {exc}")
        return jsonify({'error': 'Search service unavailable'}), 503

    if not resp.ok:
        return jsonify({'error': f'Search service returned {resp.status_code}'}), 502

    return jsonify(resp.json()), 200


@calls_bp.route('/<call_id>/kb-search-from-transcript', methods=['POST'])
@require_auth
@rate_limit('kb-search-transcript', 60, 60)
def kb_search_from_transcript(call_id):
    """KB Factbook: search KB using the last N final caller utterances as the query.

    Body: {n_utterances?: int (1-20, default 5), top_k?: int (1-20, default 5)}
    The collection is ALWAYS derived server-side from the call's queue →
    AI-agent → collection assignment (kb_collection_for_queue).
    Returns {success, collection_name, query, results, [note]}. ``note`` is set
    when there were no caller utterances to derive a query from — in that case
    results is empty but it's not a hard error.
    """
    import requests as http_requests
    from app.services.knowledge import kb_collection_for_queue

    call = None
    if call_id.isdigit():
        call = db.session.query(Call).filter_by(id=int(call_id)).first()
    if not call:
        call = Call.find_by_sid(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    data = request.get_json(silent=True) or {}
    n_utterances = data.get('n_utterances', 5)
    top_k = data.get('top_k', 5)
    collection_name = kb_collection_for_queue(call.queue_id, call.workspace_id)

    if not isinstance(n_utterances, int) or n_utterances < 1 or n_utterances > 20:
        n_utterances = 5
    if not isinstance(top_k, int) or top_k < 1 or top_k > 20:
        top_k = 5

    rows = (
        db.session.query(Transcription)
        .filter(Transcription.call_id == call.id)
        .filter(Transcription.speaker == 'caller')
        .filter(Transcription.is_final == True)
        .order_by(Transcription.created_at.desc())
        .limit(n_utterances)
        .all()
    )
    utterances = [r.transcript for r in reversed(rows) if r.transcript]
    if not utterances:
        return jsonify({
            'success': True,
            'collection_name': collection_name,
            'query': '',
            'results': [],
            'note': 'No caller utterances yet to derive a query from.',
        }), 200

    query = ' '.join(utterances)

    ai_agents_url = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')
    try:
        resp = http_requests.post(
            f"{ai_agents_url}/search",
            json={
                'collection_name': collection_name,
                'query': query,
                'top_k': top_k,
            },
            timeout=30,
        )
    except http_requests.RequestException as exc:
        logger.error(f"KB transcript-search proxy failed: {exc}")
        return jsonify({'error': 'Search service unavailable'}), 503

    if not resp.ok:
        return jsonify({'error': f'Search service returned {resp.status_code}'}), 502

    return jsonify(resp.json()), 200


def _coach_call_lookup(call_id):
    """Shared helper for the coach/* endpoints — resolve a call by id-or-SID
    and enforce the "this is your call" gate. Returns (call, user, error_response).
    On the happy path ``error_response`` is None.
    """
    call = None
    if call_id.isdigit():
        call = db.session.query(Call).filter_by(id=int(call_id)).first()
    if not call:
        call = Call.find_by_sid(call_id)
    if not call:
        return None, None, (jsonify({'error': 'Call not found'}), 404)

    user = request.current_user
    is_admin = getattr(user, 'role', None) == 'admin'
    if not is_admin and call.assigned_agent_id != user.id:
        return None, None, (jsonify({
            'error': 'Only the assigned agent on this call can control the coach.',
        }), 403)
    return call, user, None


@calls_bp.route('/<call_id>/coach/attach', methods=['POST'])
@block_in_demo_mode
@require_coach_enabled
@require_auth
@require_permission('can_use_coach')
def coach_attach(call_id):
    """AI Coach (sidecar) attach — start the sidecar with the given mode.

    Body: ``{mode: 'on_request' | 'auto'}``. Idempotent — calling attach
    again with a new mode detaches the existing sidecar and starts a fresh
    one with the new prompt. Use this for mode switches mid-call (the
    sidecar can't be reconfigured in place; we detach + re-attach).

    Authorization:
      - Capability gate: ``can_use_coach`` (admin-set, defaults vary by role)
      - Ownership gate: only the call's assigned agent (or any admin)

    Returns 202 — sidecar attach is async; the next ``coaching_suggestion``
    event is the agent's confirmation it's live.
    """
    from app.services.coach import VALID_MODES, is_active_mode
    from app.services import call_transport
    from app.utils.url_utils import get_base_url

    call, user, err = _coach_call_lookup(call_id)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    mode = (data.get('mode') or '').strip()
    if mode not in VALID_MODES:
        return jsonify({
            'error': f'mode must be one of {VALID_MODES}',
        }), 400
    if not is_active_mode(mode):
        # 'off' isn't an attach — call /detach instead. Reject explicitly so
        # client bugs don't quietly leave a stale sidecar attached.
        return jsonify({
            'error': "Use POST /coach/detach to turn the coach off.",
        }), 400

    try:
        # NOTE: previously this site called ``detach_sidecar`` first as a
        # "cheap no-op cleanup in case a previous mode is still attached".
        # That stopped being safe once ``detach_sidecar`` learned to
        # restore ``live_transcribe`` (so coach-off doesn't kill the
        # transcription panel). Calling detach then attach back-to-back
        # would now do: stop sidecar → start live_transcribe → stop
        # live_transcribe → start sidecar — a wasteful flip with a brief
        # window of double-attach errors. ``attach_sidecar`` already
        # handles all the slot-clearing internally (stops any old
        # sidecar AND any active live_transcribe before starting the new
        # sidecar), so we go straight to attach.
        call_transport.attach_sidecar(
            call=call,
            agent=user,
            mode=mode,
            queue_slug=call.queue_id or '',
            base_url=get_base_url(),
        )
    except Exception as e:
        logger.error(
            f"coach_attach: failed for call {call.signalwire_call_sid}: {e}",
            exc_info=True,
        )
        return jsonify({
            'error': 'Coach attach failed. Try again in a moment.',
            'detail': str(e),
        }), 502

    return jsonify({
        'success': True,
        'mode': mode,
        'message': f'Coach attached in {mode} mode.',
    }), 202


@calls_bp.route('/<call_id>/coach/detach', methods=['POST'])
@block_in_demo_mode
@require_coach_enabled
@require_auth
@require_permission('can_use_coach')
def coach_detach(call_id):
    """Detach the AI Coach sidecar from this call.

    Idempotent: no error when nothing's attached. Always returns 200 even
    on SignalWire-side detach failure, because from the agent's perspective
    "stop coaching" is a fire-and-forget — they don't need to retry.
    """
    from app.services import call_transport

    call, _user, err = _coach_call_lookup(call_id)
    if err:
        return err

    try:
        call_transport.detach_sidecar(call)
    except Exception as e:
        # Log but don't surface — detach failures are not actionable for
        # the agent; the sidecar will auto-terminate on call end anyway.
        logger.warning(
            f"coach_detach: SignalWire detach failed for "
            f"{call.signalwire_call_sid}: {e}"
        )

    return jsonify({'success': True, 'mode': 'off'}), 200


@calls_bp.route('/<call_id>/coach/ask', methods=['POST'])
@block_in_demo_mode
@require_coach_enabled
@require_auth
@require_permission('can_use_coach')
def coach_ask(call_id):
    """AI Coach (sidecar) ask endpoint — agent-initiated suggestion request.

    Used by the LiveCallTab Coach panel when the panel is in ``on_request``
    mode. Body: ``{question: str}``. Returns 202 with a locally-generated
    ``ask_id``; the actual answer arrives async via the sidecar webhook
    (``/api/webhooks/sidecar/events``) and gets pushed to the agent's
    call room as a ``coaching_suggestion`` event with kind=``ask_answer``.

    Authorization: only the call's currently-assigned agent (or admin)
    may ask. Observers and unrelated users get 403.

    Correlation: pushes the ask to a per-call Redis FIFO list so the
    webhook can pop-and-attach when the matching answer arrives.
    """
    call, user, err = _coach_call_lookup(call_id)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400
    if len(question) > 2000:
        return jsonify({'error': 'question is too long (max 2000 chars)'}), 400

    # Locally-generated correlation token. SignalWire may not echo this
    # yet (roadmap 2k Q3 — sidecar ask_id pending). The M10 FIFO shim
    # in webhooks.sidecar_events handles correlation as a fallback.
    ask_id = secrets.token_urlsafe(12)
    pending_entry = {
        'ask_id': ask_id,
        'question': question,
        'agent_user_id': user.id,
        'ts': datetime.utcnow().isoformat(),
    }

    # Push to per-call FIFO so the answer can be matched. Best-effort: if
    # Redis is down we still send the ask, just lose correlation metadata.
    try:
        from app.services.redis_service import get_redis_client
        r = get_redis_client()
        if r is not None:
            key = f"coach_pending_asks:{call.signalwire_call_sid}"
            r.rpush(key, json.dumps(pending_entry))
            # Cap pending depth (a chatty agent shouldn't OOM Redis) and
            # expire so finished calls don't leak keys.
            r.ltrim(key, -20, -1)
            r.expire(key, 3600)
    except Exception as e:
        logger.warning(
            f"coach_ask: failed to push pending entry to Redis (non-fatal): {e}"
        )

    # Fire the ask. Async — answer arrives via webhook.
    try:
        sw_api = get_signalwire_api()
        sw_api.ask_ai_sidecar(
            call.signalwire_call_sid, question, ask_id=ask_id,
        )
    except Exception as e:
        logger.error(
            f"coach_ask: SignalWire ask failed for call "
            f"{call.signalwire_call_sid}: {e}"
        )
        return jsonify({
            'error': 'Coach is unavailable right now. Try again in a moment.',
            'detail': str(e),
        }), 502

    logger.info(
        f"Coach ask sent: call={call.signalwire_call_sid} "
        f"agent={user.id} ask_id={ask_id} q='{question[:80]}'"
    )
    return jsonify({
        'success': True,
        'ask_id': ask_id,
        'message': 'Coach is thinking — answer will arrive in the panel shortly.',
    }), 202


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
        agent_id = request.args.get('agent_id', type=int)  # Filter by assigned agent

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
        # For other calls, only show user's own calls.
        #
        # ISO-13 (2026-07-07 pre-deploy): the unfiltered ai_active list
        # returns every visitor's live AI call WITH full transcripts. That
        # cross-visitor visibility is the hosted demo's explicit design
        # ("watch the floor"), but on a clone-and-own deployment a regular
        # agent should not see calls they're not on. So: in demo mode keep
        # the all-visible behavior; otherwise restrict the unfiltered view to
        # supervisors/admins and fall back to own-calls for regular agents.
        role = request.current_user.role or ''
        show_all_ai = status_filters and 'ai_active' in status_filters and (
            role in ('admin', 'supervisor')
        )
        if show_all_ai:
            # Unfiltered within the caller's scope: hosted visitors are the
            # admin of their own workspace, so the tenancy auto-filter keeps
            # this to their rows; clone-and-own admins/supervisors see all,
            # same as baseline. (The old demo-persona floor filter is gone
            # with the shared floor.)
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

        # Filter by assigned agent if provided
        if agent_id:
            query = query.filter(Call.assigned_agent_id == agent_id)

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
        # 'answered' mislabels human-answered calls as ai_active here; AI
        # handoffs set ai_active explicitly (swml.py, ai_control.py), and
        # Call.handler_type is the real AI-vs-human source of truth. Kept
        # until the dashboard status vocabulary grows a human_active state.
        'answered': 'ai_active',
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

        # ISO-4: ending a call is a mutation across all visitors' calls —
        # owner/privileged only (this is the ISO-1-class harm scoped to a
        # single call by id).
        owner_check = _require_call_ownership(call, request.current_user)
        if owner_check is not None:
            return owner_check

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

        # The agent explicitly pressed the hangup button (this endpoint is
        # only hit from the agent's UI), so claim the hangup_direction on
        # their behalf — but ONLY if the call-state webhook hasn't already
        # set it (caller hung up first and the webhook beat us here). This
        # is what drives the agent_hangup vs caller_hangup chip in the
        # call-history list.
        if not call.hangup_direction:
            call.hangup_direction = 'agent'

        # Always update call status and emit events regardless of SignalWire API result
        call.update_status('completed')
        call.ended_at = call.ended_at or datetime.utcnow()
        if call.answered_at and not call.duration:
            call.duration = int((call.ended_at - call.answered_at).total_seconds())
        # Close any open legs here too — this API-driven end does NOT reliably
        # trigger SignalWire's 'ended' webhook, so without this the legs (and the
        # call timeline) stay 'active' on a completed call.
        from app.models.call_leg import CallLeg
        CallLeg.end_all_open(call.id, reason='hangup')
        db.session.commit()
        logger.info(f"Call status updated to 'completed' in database")

        # Redis queue cleanup — /end sets status='completed' directly, which does
        # NOT trigger the webhook cleanup path (that fires on SignalWire's 'ended'
        # status callback, which doesn't always reliably fire after an API-driven
        # end). Without this, the call lingers in queue:{slug} Redis sets,
        # inflating queue position counts on subsequent calls.
        try:
            from app.services.queue_service import QueueService
            from app.services.redis_service import get_redis_client
            rdb = get_redis_client()
            if rdb and call.signalwire_call_sid:
                QueueService(rdb).remove_call_from_all_queues(call.signalwire_call_sid)
        except Exception as cleanup_err:
            logger.warning(f"Redis queue cleanup failed after /end: {cleanup_err}")

        # Emit call update so frontend removes from active list
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        # Also emit call_ended for comprehensive UI cleanup (matches webhook pattern)
        from app import socketio
        from app.services.ws_rooms import workspace_room
        call_ended_data = {
            'callId': call.id,
            'call_sid': call.signalwire_call_sid,
            'conference_name': call.conference_name,
            'reset_ui': True
        }
        socketio.emit('call_ended', call_ended_data,
                      room=workspace_room(call.workspace_id))

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

        # ISO-4: read gate — owner/privileged, or (demo) a persona viewing an
        # AI-handled call. The transcript is the most sensitive read surface.
        owner_check = _require_call_ownership(
            call, request.current_user, allow_demo_ai_view=True
        )
        if owner_check is not None:
            return owner_check

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

        # ISO-2 (2026-07-07 pre-deploy): this injects a message the live AI
        # agent immediately speaks/acts on. Without an ownership check any
        # authenticated user (incl. a leased demo persona) could steer
        # another visitor's AI call by guessing an enumerable call id. Only
        # the call's owner/assigned agent — or a supervisor/admin — may do
        # this, mirroring ai_control.inject_system_message's gate.
        owner_check = _require_call_ownership(call, request.current_user)
        if owner_check is not None:
            return owner_check

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
@require_internal_auth
def register_ai_leg(call_db_id):
    """Register the AI agent's B-leg call SID for later use (e.g., takeover).

    Called by the AI agent's capture_base_url callback when it starts handling
    a call. ISO-9 (2026-07-07 pre-deploy): now requires internal HTTP Basic
    auth (WEBHOOK_AUTH creds) — previously unauthenticated and publicly
    reachable via nginx `location /api`, so anyone could corrupt takeover
    routing by registering a bogus B-leg SID on an enumerable call id.

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
@require_internal_auth
def report_sentiment(call_db_id):
    """Receive real-time sentiment updates from AI agents during a call.

    Called by the AI agent's report_sentiment SWAIG tool (fire-and-forget).
    ISO-9 (2026-07-07 pre-deploy): now requires internal HTTP Basic auth
    (WEBHOOK_AUTH creds) — previously unauthenticated and publicly reachable,
    so anyone could set an arbitrary sentiment on any call by id (and trigger
    the unroomed sentiment_update broadcast + contact-average recompute).

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

        # To the call's workspace — its watchers + assigned agent (§8.1).
        from app.services.ws_rooms import workspace_room
        socketio.emit('sentiment_update', sentiment_data,
                      room=workspace_room(call.workspace_id))

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

        # Emit event to notify UI. join_call joins the BARE sid as the
        # room name — the old 'call_' prefix targeted a room nobody joins.
        socketio.emit('call_takeover_initiated', {
            'call_sid': call.signalwire_call_sid,
            'call_id': call.id,
            'agent_id': request.current_user.id,
            'leg_id': new_leg.id
        }, room=call.signalwire_call_sid)

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

        # Atomic claim — match the push-dispatch race fix. Two agents
        # clicking Take simultaneously (or one clicking Take while
        # push-dispatch is mid-claim for someone else) used to do
        # check-then-act on `assigned_agent_id`, with each agent's UI
        # convinced they had ownership but only one DB row state. The
        # UPDATE...WHERE clause makes it impossible to grab a call that's
        # already pinned to a different agent. The OR-equal clause keeps
        # Take idempotent for the assigned agent — refreshing the page or
        # double-clicking won't 409 them.
        from sqlalchemy import text
        user_id = request.current_user.id
        claim_at = datetime.utcnow()
        claim = db.session.execute(
            text(
                "UPDATE calls SET "
                "  assigned_agent_id = :uid, "
                "  assigned_at = COALESCE(assigned_at, :ts), "
                "  status = 'assigned', "
                "  handler_type = 'human', "
                "  user_id = :uid, "
                "  conference_name = COALESCE(conference_name, :conf) "
                "WHERE id = :id AND ("
                "  assigned_agent_id IS NULL OR assigned_agent_id = :uid"
                ") RETURNING id"
            ),
            {
                'uid': user_id,
                'ts': claim_at,
                'id': call.id,
                'conf': f"interaction-{call.signalwire_call_sid}",
            },
        )
        if not claim.fetchone():
            db.session.rollback()
            # Re-fetch to log who actually owns it (without trusting the
            # potentially-stale in-memory call.assigned_agent_id).
            current = db.session.query(Call.assigned_agent_id).filter_by(id=call.id).scalar()
            logger.warning(
                f"Take: call {call_id} is assigned to agent {current} "
                f"(requesting user {user_id})"
            )
            return jsonify({'error': 'Call is assigned to another agent'}), 409

        from app.services.interaction_timeline import best_effort, record_queue_offered
        best_effort(record_queue_offered, call, user_id, claim_at)
        db.session.commit()
        # Refresh so downstream code (emit, response payload) reads the
        # claimed state, not the pre-claim snapshot.
        db.session.refresh(call)

        # Mirror what the auto-dispatch paths do after a successful claim:
        # (1) mark the agent busy in Redis so router doesn't dispatch them
        # to another call, AND (2) remove this call from the queue zsets so
        # router doesn't try to re-pop the SAME call onto another agent
        # who just went available.
        #
        # The manual Take path historically did neither, causing:
        #   - agents staying in agents:available → double-dispatch onto an
        #     agent already on a call
        #   - taken calls lingering in queue zsets → inflated queue-depth
        #     metrics + spurious "Assigned to you" banners for newly-available
        #     agents (they 409 on claim per the SEC race fix, but the
        #     banner-then-failed-take UX is bad)
        #   - caller-hangup / watchdog release guards never matching (they
        #     key off current_call_id == signalwire_call_sid)
        #
        # 2026-06-02 audit (LIFE-01) shipped the busy-mark but missed the
        # queue removal. Fixing both here on the same Redis handle.
        # Best-effort throughout: a Redis hiccup must not fail an otherwise-
        # successful claim.
        try:
            from app.services.redis_service import get_redis_client
            rdb = get_redis_client()
            if rdb:
                qs = QueueService(rdb)
                qs.set_agent_status(
                    str(request.current_user.id),
                    'busy',
                    current_call_id=call.signalwire_call_sid,
                )
                # Remove from queue zsets so push-dispatch can't re-pop this
                # sid onto another agent going available.
                try:
                    qs.remove_call_from_all_queues(call.signalwire_call_sid)
                except Exception as remove_err:
                    logger.warning(
                        f"Take: failed to dequeue call {call.signalwire_call_sid} "
                        f"after claim: {remove_err}"
                    )
        except Exception as e:
            logger.warning(
                f"Take: failed to mark agent {request.current_user.id} busy: {e}"
            )

        logger.info(f"Call {call_id} taken by agent {request.current_user.id}, conference: {call.conference_name}")

        # Emit queue update to remove from the workspace's queue displays
        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': call.queue_id,
            'action': 'taken',
            'taken_by_agent_id': request.current_user.id
        }, room=workspace_room(call.workspace_id))

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

        # ISO-4: mutates status + flips handler_type to human — owner/
        # privileged only (a non-owner could force-flip another visitor's
        # AI call to human-handled and disrupt it).
        owner_check = _require_call_ownership(call, request.current_user)
        if owner_check is not None:
            return owner_check

        old_status = call.status

        # Update status through the model so the durable handling timeline is
        # written in the same transaction as the compatibility Call fields.
        call.handler_type = 'human'  # Agent is now handling

        # If becoming active, mark answered time
        if new_status == 'active' and not call.answered_at:
            call.answered_at = datetime.utcnow()

        call.update_status(new_status)

        db.session.commit()
        logger.info(f"Call {call_id} status updated: {old_status} -> {new_status}")

        # Emit update to the call's workspace
        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('call_update', {
            'call': call.to_dict(include_contact=True)
        }, room=workspace_room(call.workspace_id))

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


@calls_bp.route('/<call_id>/timeline', methods=['GET'])
@require_auth
def get_call_timeline(call_id):
    """Return the measured queue and handling history for one interaction."""
    call = None
    if str(call_id).isdigit():
        call = db.session.query(Call).filter_by(id=int(call_id)).first()
    if not call:
        call = Call.find_by_sid(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check is not None:
        return owner_check

    from app.models import HandlingSegment, Queue, QueueAttempt, User
    attempts = (
        QueueAttempt.query.filter_by(call_id=call.id)
        .order_by(QueueAttempt.attempt_number.asc())
        .all()
    )
    segments = (
        HandlingSegment.query.filter_by(call_id=call.id)
        .order_by(HandlingSegment.started_at.asc())
        .all()
    )
    agent_ids = {
        agent_id
        for attempt in attempts
        for agent_id in (
            attempt.last_offered_agent_id,
            attempt.last_declined_agent_id,
            attempt.accepted_agent_id,
        )
        if agent_id is not None
    } | {
        segment.agent_id for segment in segments if segment.agent_id is not None
    }
    agents = {
        user.id: (user.name or user.email)
        for user in User.query.filter(User.id.in_(agent_ids)).all()
    } if agent_ids else {}
    queue_ids = {attempt.queue_id for attempt in attempts if attempt.queue_id is not None}
    queues = {
        queue.id: queue.display_name
        for queue in Queue.query.filter(Queue.id.in_(queue_ids)).all()
    } if queue_ids else {}

    attempt_rows = []
    for attempt in attempts:
        row = attempt.to_dict()
        row.update({
            'queueDisplayName': queues.get(attempt.queue_id),
            'lastOfferedAgentName': agents.get(attempt.last_offered_agent_id),
            'lastDeclinedAgentName': agents.get(attempt.last_declined_agent_id),
            'acceptedAgentName': agents.get(attempt.accepted_agent_id),
        })
        attempt_rows.append(row)
    segment_rows = []
    for segment in segments:
        row = segment.to_dict()
        row['agentName'] = agents.get(segment.agent_id)
        segment_rows.append(row)

    return jsonify({
        'callId': call.id,
        'signalwireCallId': call.signalwire_call_sid,
        'transport': call.transport,
        'queueAttempts': attempt_rows,
        'handlingSegments': segment_rows,
    })


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

        # ISO-14/demo: agent notes are free text a visitor types and that
        # surfaces in the shared CRM — moderate them in demo mode, same as
        # contact fields and AI-message injects.
        if is_demo_mode() and agent_notes:
            ok, reason = is_text_acceptable(agent_notes)
            if not ok:
                return jsonify({
                    'error': reason,
                    'code': 'moderation_blocked',
                    'field': 'agent_notes',
                }), 422

        # Look up by numeric ID first, then SignalWire call_sid.
        call = None
        if str(call_id).isdigit():
            call = db.session.query(Call).filter_by(id=int(call_id)).first()
        if not call:
            call = Call.find_by_sid(call_id)
        if not call:
            return jsonify({'error': 'Call not found'}), 404

        # ISO-4: wrap-up writes notes/disposition onto a call — owner/
        # privileged only (was writable on ANY call by enumerable id).
        owner_check = _require_call_ownership(call, request.current_user)
        if owner_check is not None:
            return owner_check

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
            # A human edited the wrap-up — claim provenance so the
            # "Captured by AI" badge turns off for good (not just this session).
            call.wrap_up_source = 'agent'
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
                'wrap_up_source': call.wrap_up_source,
            },
        }), 200

    except Exception as e:
        logger.error(f"Failed to save wrap-up: {str(e)}")
        return jsonify({'error': f'Failed to save wrap-up: {str(e)}'}), 500


@calls_bp.route('/cleanup-stale', methods=['POST'])
@require_auth
@require_role('supervisor', 'admin')
def cleanup_stale_calls():
    """Clean up stale calls that are stuck in ringing/active status.

    Marks calls as 'ended' if they've been in ringing/active status for too long.
    This handles cases where webhooks didn't fire properly.

    ISO-1 (2026-07-07 pre-deploy): this ends live calls across ALL users, so
    it's restricted to supervisor/admin (was @require_auth only — any leased
    demo persona could have ended every visitor's call with one request). The
    ``force=true`` shortcut (end EVERY non-terminal call regardless of age) is
    additionally blocked in demo mode — even a real supervisor shouldn't be
    able to nuke every visitor's in-progress call on the shared instance.

    Query params:
    - force=true: Clean ALL non-terminal calls regardless of age (for dev)
    - max_age_minutes=N: Override the default 60 minute threshold
    """
    try:
        force = request.args.get('force', 'false').lower() == 'true'
        max_age_minutes = request.args.get('max_age_minutes', 60, type=int)

        # Phase 2 gate retriage (§9): force cleanup is fine INSIDE a
        # workspace (the calls query below runs auto-scoped, so a visitor
        # admin only nukes their own in-progress calls) but stays blocked
        # for unscoped platform users in hosted mode — one request would
        # end every visitor's live call.
        if (
            force and is_demo_mode()
            and getattr(request.current_user, 'workspace_id', None) is None
        ):
            return jsonify({
                'error': 'Force cleanup is disabled platform-wide on the hosted demo.',
                'code': 'demo_blocked',
            }), 403

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
            call.update_status('ended')
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
        # @require_auth sets request.current_user (a User object). The
        # previous code used request.user_id which doesn't exist — every
        # call to /api/calls/my-stats 500'd silently, polluting the log
        # and breaking the dashboard agent-stats panel.
        user_id = request.current_user.id
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        from app.services.interaction_timeline import get_agent_performance
        performance = get_agent_performance(
            today_start, workspace_id=request.current_user.workspace_id,
        ).get(user_id, {})
        calls_today = performance.get('calls_handled', 0)
        avg_handle_time = performance.get('average_handle_time', 0)

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
