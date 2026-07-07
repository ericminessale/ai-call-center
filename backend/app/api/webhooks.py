from flask import request, jsonify
from app import db, socketio
from app.api import webhooks_bp
from app.models import Call, CallLeg, Transcription, WebhookEvent
from app.models.user import User
from app.services.redis_service import publish_event
from app.utils.webhook_auth import require_webhook_auth
from datetime import datetime
from typing import Optional
import logging
import json
import os
import threading

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telemetry capture (debugging aid)
# ---------------------------------------------------------------------------
# Persist raw SignalWire webhook payloads to host-visible files so a single
# test call can be inspected offline. The backend container bind-mounts
# ./backend -> /app (see docker-compose.yml), so anything under _CAPTURE_DIR
# shows up on the host at signalwire-call-center/backend/captures/.
#
# Two streams are written per kind:
#   <kind>-latest.json : the most recent payload, pretty-printed (overwritten)
#   <kind>.jsonl       : append-only history, one JSON record per line
#
# Best-effort: capture failures are logged and swallowed so they never break
# real webhook handling. The lock serialises writes within this worker
# process; across gunicorn workers each record is written in a single write()
# and carries a microsecond `captured_at`, so a high-volume (debug level 2)
# burst can be re-sorted by timestamp if lines from two workers interleave.
_CAPTURE_DIR = os.getenv('WEBHOOK_CAPTURE_DIR') or os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'captures'))
_capture_lock = threading.Lock()


def capture_webhook_payload(kind: str, payload, *, latest: bool = True,
                            stream: bool = True) -> None:
    """Write a webhook payload to captures/ for offline debugging."""
    try:
        record = {
            'captured_at': datetime.utcnow().isoformat() + 'Z',
            'payload': payload,
        }
        with _capture_lock:
            os.makedirs(_CAPTURE_DIR, exist_ok=True)
            if latest:
                latest_path = os.path.join(_CAPTURE_DIR, f'{kind}-latest.json')
                with open(latest_path, 'w', encoding='utf-8') as fh:
                    fh.write(json.dumps(record, default=str, indent=2))
            if stream:
                stream_path = os.path.join(_CAPTURE_DIR, f'{kind}.jsonl')
                with open(stream_path, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(record, default=str) + '\n')
    except Exception:
        logger.exception("Failed to capture %s webhook payload", kind)


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


def _trigger_kb_auto_search_if_enabled(call, call_sid_str):
    """Auto-fire a KB search when a caller's turn ends, if the assigned human
    agent has `kb_factbook_mode='auto'`. Debounced via Redis (3s) so chained
    caller utterances don't trigger a search per turn. Emits ``kb_fact`` via
    Socket.IO to the call room. Best-effort — caller catches all exceptions.
    """
    import requests as http_requests
    from app.services.redis_service import get_redis_client

    agent = User.query.get(call.assigned_agent_id) if call.assigned_agent_id else None
    if not agent or agent.kb_factbook_mode != 'auto':
        return

    redis_client = get_redis_client()
    if redis_client:
        acquired = redis_client.set(
            f"kb_factbook_debounce:{call.id}", '1', ex=3, nx=True
        )
        if not acquired:
            return

    rows = (
        db.session.query(Transcription)
        .filter(Transcription.call_id == call.id)
        .filter(Transcription.speaker == 'caller')
        .filter(Transcription.is_final == True)
        .order_by(Transcription.created_at.desc())
        .limit(5)
        .all()
    )
    utterances = [r.transcript for r in reversed(rows) if r.transcript]
    if not utterances:
        return
    query = ' '.join(utterances)

    ai_agents_url = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')
    try:
        resp = http_requests.post(
            f"{ai_agents_url}/search",
            json={
                'collection_name': 'sales_knowledge',  # TODO: derive from queue / agent assignment
                'query': query,
                'top_k': 3,
            },
            timeout=10,
        )
    except http_requests.RequestException as exc:
        logger.warning(f"[Factbook auto] /search request failed: {exc}")
        return
    if not resp.ok:
        return

    payload = resp.json()
    socketio.emit('kb_fact', {
        'call_sid': call_sid_str,
        'query': query,
        'results': payload.get('results', []),
        'collection_name': payload.get('collection_name'),
    }, room=call_sid_str)
    logger.info(
        f"[Factbook auto] emitted {len(payload.get('results', []))} facts for call {call_sid_str}"
    )


@webhooks_bp.route('/call-status', methods=['POST'])
@require_webhook_auth
def call_status():
    """Handle call status webhook from SignalWire (both CallStatus and CallState events)."""
    try:
        # Get webhook data - handle both form data and JSON
        data = request.form.to_dict() if request.form else request.get_json()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/call-status")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # Check if this is a call state event with the new format
        from_number = None
        if 'params' in data and 'call_state' in data['params']:
            # New SignalWire SWML webhook format with params object
            params = data.get('params', {})
            call_id = params.get('call_id')
            status = params.get('call_state')
            from_number = params.get('from', params.get('from_number'))

            # Log for debugging
            logger.info(f"Extracted (params format) - Call ID: {call_id}, Status: {status}, From: {from_number}")
        elif 'call' in data:
            # Alternative SignalWire format with call object
            call_data = data.get('call', {})
            call_id = call_data.get('call_id')
            status = call_data.get('call_state')
            from_number = call_data.get('from', call_data.get('from_number'))

            # Log for debugging
            logger.info(f"Extracted (call format) - Call ID: {call_id}, Status: {status}, From: {from_number}")
        else:
            # Old format or Twilio SDK format
            call_id = data.get('CallSid') or data.get('call_sid') or data.get('call_id')
            status = data.get('CallStatus') or data.get('CallState') or data.get('status') or data.get('state')
            from_number = data.get('From') or data.get('from') or data.get('from_number')

        logger.info(f"Call status webhook: {call_id} - {status} - From: {from_number}")

        # Update call in database FIRST (to get the database ID)
        call = Call.find_by_sid(call_id)
        if call:
            # Map SWML call states to our internal status
            # SWML only sends: created, ringing, answered, ended
            status_mapping = {
                'created': 'created',
                'ringing': 'ringing',
                'answered': 'answered',
                'ended': 'ended'
            }

            mapped_status = status_mapping.get(status.lower(), status)

            # Promotion gate for 'pending' calls.
            # /direct-inbound writes status='pending' optimistically when its
            # webhook hits — the call may never actually progress (carrier
            # auto-retry storms can fire the webhook 8x in 19s from a single
            # failed dial). Promote 'pending' -> 'waiting' only when the
            # call leg actually establishes ('created'/'ringing'/'answered'):
            # at that point we know the PSTN call is alive and our SWML is
            # running. Otherwise the call stays pending and never shows in
            # the queue UI. On 'ended', mark ended directly so the watchdog
            # doesn't have to.
            was_pending = (call.status == 'pending')
            if was_pending and mapped_status in ('created', 'ringing', 'answered'):
                call.status = 'waiting'
                if mapped_status == 'answered' and not call.answered_at:
                    call.answered_at = datetime.utcnow()
                logger.info(
                    f"Promoted pending call {call_id} -> waiting on '{mapped_status}'"
                )
            else:
                # On 'ended', try to capture who hung up first from the
                # SignalWire payload before update_status runs (so
                # compute_end_reason sees hangup_direction). The field name
                # SignalWire uses for inbound SWML call-state isn't formally
                # documented per-event, so we check several plausible keys
                # defensively. The frontend may also have already set this
                # via /api/calls/<id>/hangup-direction (agent-pressed-end
                # button) — don't overwrite that more-authoritative signal.
                if mapped_status == 'ended' and not call.hangup_direction:
                    src = {}
                    if isinstance(data, dict):
                        src.update(data)
                        if isinstance(data.get('params'), dict):
                            src.update(data['params'])
                        if isinstance(data.get('call'), dict):
                            src.update(data['call'])
                    raw_dir = (
                        src.get('hangup_direction')
                        or src.get('hangup_disposition')  # FreeSWITCH style
                        or src.get('end_source')
                        or src.get('ended_by')
                    )
                    if raw_dir:
                        raw = str(raw_dir).lower()
                        # FreeSWITCH 'send_bye' = our side ended → agent;
                        # 'recv_bye' = peer ended → caller. Plus literal
                        # 'caller'/'callee'/'agent' variants.
                        if raw in ('caller', 'callee_hangup', 'recv_bye'):
                            call.hangup_direction = 'caller'
                        elif raw in ('callee', 'agent', 'caller_hangup', 'send_bye'):
                            # 'callee' here = the called party = the agent leg
                            # that received the dial.
                            call.hangup_direction = 'agent'
                        logger.info(
                            f"call-status: captured hangup_direction={call.hangup_direction!r} "
                            f"from raw {raw_dir!r}"
                        )
                call.update_status(mapped_status)

            # Update from_number if provided and not already set
            if from_number and not call.from_number:
                call.from_number = from_number
                logger.info(f"Updated call {call_id} with from_number: {from_number}")

            db.session.commit()

            # On the first promotion from pending, tell the Queue tab to
            # add this call. Queue-status webhook ('entering') would also do
            # this — emit-or-update via the action='added' handler is
            # idempotent (dedups by call.id) so a double-fire is harmless.
            if was_pending and call.status == 'waiting':
                try:
                    socketio.emit('queue_update', {
                        'call': call.to_dict(include_contact=True),
                        'queue_id': call.queue_id,
                        'action': 'added',
                    })
                    logger.info(
                        f"Emitted queue_update added for promoted call {call_id}"
                    )
                except Exception as e:
                    logger.warning(f"call_status: queue_update added emit failed: {e}")

            # Log the webhook event (using database call.id, not SignalWire call_id)
            WebhookEvent.log_event(
                event_type=f"call_status_{status}",
                payload=data,
                call_id=call.id  # Use database ID
            )

            # Map to dashboard status
            dashboard_status = map_to_dashboard_status(mapped_status)

            # Emit status update via WebSocket with full call context
            # Use from_number as phoneNumber if available, otherwise fallback to destination
            phone_number = call.from_number or call.destination

            call_data = {
                'id': call.id,  # Use database UUID, not SignalWire call_id
                'call_sid': call_id,  # Also provide SignalWire ID for reference
                'phoneNumber': phone_number,  # Caller's number for inbound, destination for outbound
                'from_number': call.from_number,  # Explicitly include for clarity
                'status': dashboard_status,  # Use dashboard-friendly status
                'internal_status': mapped_status,  # Keep internal status for debugging
                'destination': call.destination,
                'destination_type': call.destination_type,
                'transcription_active': call.transcription_active,
                'conference_name': call.conference_name,  # Needed for frontend SDK cleanup on call end
                'startTime': call.created_at.isoformat() if call.created_at else None,
                'created_at': call.created_at.isoformat() if call.created_at else None,
                'answered_at': call.answered_at.isoformat() if call.answered_at else None,
                'ended_at': call.ended_at.isoformat() if call.ended_at else None,
                'user_id': call.user_id,
                'assigned_agent_id': call.assigned_agent_id,
                'queueId': call.queue_id or 'general'
            }

            # Emit to call-specific room
            socketio.emit('call_status', call_data, room=call_id)

            # Also emit to user room for CallsList updates
            if call.user_id:
                socketio.emit('call_status', call_data, room=str(call.user_id))

            # Emit call_update for Agent Dashboard
            # For AI-active calls, broadcast to ALL agents so they can see and take over
            # For human-handled calls, emit to the specific user's room
            if dashboard_status == 'ai_active' or not call.user_id:
                logger.info(f"Broadcasting call_update to all agents (status: {dashboard_status}, user_id: {call.user_id})")
                socketio.emit('call_update', {'call': call_data})  # Broadcast to all
            else:
                socketio.emit('call_update', {'call': call_data}, room=str(call.user_id))

            # Special handling for ended status to reset UI
            if mapped_status == 'ended':
                # Clean up any queue entries for this call
                from app.services.queue_service import QueueService
                from app.services.redis_service import get_redis_client
                redis_client = get_redis_client()
                if redis_client:
                    queue_svc = QueueService(redis_client)
                    queue_svc.remove_call_from_all_queues(call_id)

                    # If call was assigned to an agent, check if they're still
                    # marked busy for THIS call (meaning they never accepted it).
                    # Revert them to available so they can take new calls.
                    if call.assigned_agent_id:
                        agent_status = queue_svc.get_agent_status(str(call.assigned_agent_id))
                        if agent_status and agent_status.get('status') == 'busy' and agent_status.get('current_call_id') == call_id:
                            queue_svc.set_agent_status(str(call.assigned_agent_id), 'available')
                            logger.info(f"Reverted agent {call.assigned_agent_id} to available (caller hung up before acceptance)")

                # Close ALL active/connecting call legs — a call can have more
                # than one open leg (e.g. a conference AI leg alongside the
                # customer leg), and closing only the first left the rest stuck
                # 'active' forever, which the timeline rendered as 'Active' on a
                # finished call.
                closed_count = CallLeg.end_all_open(call.id, reason='hangup')
                if closed_count:
                    db.session.commit()
                    logger.info(f"Closed {closed_count} open leg(s) for call {call.id}")

                # Mark the conference row as ended too. Same negligence pattern
                # as Bug B: prior code left Conference rows in 'active' forever
                # because nothing wrote the terminal status on call end. The
                # end_conference helper also cascades to participants.leave().
                # No-op when the call had no conference (bridge mode + native
                # enter_queue calls have conference_name=NULL).
                if call.conference_name:
                    try:
                        from app.models import Conference
                        conf = Conference.get_active_by_name(call.conference_name)
                        if conf:
                            conf.end_conference()
                            db.session.commit()
                            logger.info(
                                f"Marked conference {call.conference_name} as ended "
                                f"(cascaded participants.leave())"
                            )
                    except Exception as e:
                        db.session.rollback()
                        logger.warning(
                            f"call_status: Conference end failed for "
                            f"{call.conference_name}: {e}"
                        )

                call_ended_data = {
                    'callId': call.id,  # Use database ID
                    'call_sid': call_id,  # Also provide SignalWire ID
                    'conference_name': call.conference_name,  # For frontend conference cleanup
                    'assigned_agent_id': call.assigned_agent_id,
                    'reset_ui': True
                }
                # Emit to user room
                socketio.emit('call_ended', call_ended_data, room=str(call.user_id))
                # Also emit to all (for AI calls visible to all agents)
                socketio.emit('call_ended', call_ended_data)
                # And emit call_update with ended status to all
                socketio.emit('call_update', {'call': call_data})

                # Tell the Queue tab to drop this call. The frontend Queue list
                # listens for `queue_update action='ended'` to remove rows;
                # without this emit, callers who hung up while parked stay
                # visible in the queue forever (Bug B, 2026-05-13).
                # Always emit — the frontend filters by call.queue_id so
                # un-queued calls are harmlessly ignored.
                try:
                    socketio.emit('queue_update', {
                        'call': call_data,
                        'queue_id': call.queue_id,
                        'action': 'ended',
                    })
                except Exception as e:
                    logger.warning(f"call_status: queue_update emit failed: {e}")

        return '', 200

    except Exception as e:
        logger.error(f"Error processing call status webhook: {str(e)}")
        return '', 500


@webhooks_bp.route('/call-heartbeat', methods=['GET', 'POST'])
def call_heartbeat():
    """Liveness heartbeat for SWML scripts.

    SignalWire's phone-number-level ``call_status_callback_url`` does not fire
    for SWML scripts (only for cXML/laml_webhooks handlers), and the SWML
    ``set`` verb's ``call_state_url`` is just a script variable — not a real
    webhook subscription. With no native hangup callback we'd never know
    when a parked caller dropped, so the bridge's hold loop fires this
    endpoint on every iteration as a liveness signal.

    Mechanism:
    - Hold loop SWML: play TTS → silence:25 → request GET /call-heartbeat → goto wait
    - Each request resets a Redis TTL key (``call_heartbeat:<call_sid>``, 90s)
    - The watchdog (``app.services.call_watchdog``) scans 'waiting'/'assigned'
      Call rows; any whose heartbeat key has expired is treated as dropped
      and the standard end-of-call cleanup runs (mark ended, Redis dequeue,
      release agent, emit ``queue_update action='ended'``).

    Auth: intentionally none. Knowledge of the call_sid (which is opaque) is
    the only secret. Worst-case spoof keeps a stale row alive for 90s — bad
    actor can't actually disrupt a live call.
    """
    try:
        call_sid = request.args.get('call_sid') or (
            request.json.get('call_sid') if request.is_json else None
        )
        if not call_sid:
            return jsonify({'ok': False, 'error': 'call_sid required'}), 400

        from app.services.redis_service import get_redis_client
        redis_client = get_redis_client()
        if redis_client:
            redis_client.set(f"call_heartbeat:{call_sid}", '1', ex=90)

        # Return empty SWML so the request verb can `save_variables` cleanly
        # and the script continues. An empty 200 also works.
        return jsonify({'ok': True}), 200
    except Exception as e:
        logger.error(f"call_heartbeat error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


def _extract_conversation_summary(data):
    """Pull the end-of-session AI summary text from a live_transcribe
    conversation_log event (start param ai_summary:true). Returns None when this
    isn't a summary event. Defensive across payload nesting since the exact
    envelope for conversation_summary is platform-controlled."""
    if not isinstance(data, dict):
        return None

    def _coerce(v):
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            t = v.get('text') or v.get('content') or v.get('summary')
            return t.strip() if isinstance(t, str) and t.strip() else None
        return None

    containers = [data, data.get('params'), data.get('channel_data')]
    if isinstance(data.get('conversation_log'), dict):
        containers.append(data['conversation_log'])
    for container in containers:
        if isinstance(container, dict):
            for key in ('conversation_summary', 'ai_summary'):
                got = _coerce(container.get(key))
                if got:
                    return got
    return None


def _apply_ai_wrapup_summary(data, summary_text):
    """Fold the native end-of-session live_transcribe summary into the call's
    wrap-up NOTES with AI provenance — unless a human has already taken over.

    This fires for the CONFERENCE/human leg (the AI leg has no ai_summary; its
    triage is captured by the AI post-prompt, which seeds agent_notes first). We
    APPEND the human-leg summary onto that AI-triage base so the wrap-up covers
    the WHOLE call, and only act on the FINAL end-of-call summary (call_end_date
    set) so mid-call interim fragments never overwrite the final note."""
    channel_data = data.get('channel_data') if isinstance(data.get('channel_data'), dict) else {}
    call_info = data.get('call_info') if isinstance(data.get('call_info'), dict) else {}
    call_id = (call_info or {}).get('call_id') or data.get('call_id') or (channel_data or {}).get('call_id')

    call = Call.find_by_sid(call_id) if call_id else None
    # Log every conversation_log event (interim + final) for debuggability.
    WebhookEvent.log_event(
        event_type="transcription_summary",
        payload=data,
        call_id=call.id if call else None,
    )
    if not call:
        logger.warning(f"[transcription] conversation_summary for unknown call {call_id}")
        return

    # Only the END-of-call summary is the wrap-up. SignalWire also emits interim
    # conversation_log events mid-call with call_end_date=0 — skip those so a
    # partial summary never overwrites the final whole-call note.
    call_end_date = (channel_data or {}).get('call_end_date')
    if call_end_date is not None and not call_end_date:
        logger.info(f"conversation_summary: interim event (call_end_date=0) for call {call.id}; not writing")
        return

    # Never clobber a human who has already edited/saved the wrap-up.
    if call.wrap_up_source == 'agent':
        logger.info(f"conversation_summary: call {call.id} wrap-up is human-owned; skipping")
        return

    existing = (call.agent_notes or '').strip()
    if summary_text in existing:
        # Duplicate final event — already folded in. Idempotent no-op.
        return
    if existing:
        # The AI post-prompt already wrote the AI-triage note; append the
        # human-leg summary so the wrap-up reflects the whole call, not just
        # the post-handoff portion.
        call.agent_notes = f"{existing}\n\nAfter handoff to a human agent: {summary_text}"
    else:
        # AI-only call, or no post-prompt note — the human-leg summary stands alone.
        call.agent_notes = summary_text
    call.wrap_up_source = 'ai'
    db.session.commit()
    logger.info(
        f"conversation_summary -> wrap-up notes for call {call.id} "
        f"(combined={bool(existing)}, {len(summary_text)} chars)"
    )

    try:
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)
    except Exception as e:
        logger.warning(f"conversation_summary emit failed: {e}")


def _process_utterance_event(data, *, source: str) -> tuple:
    """Persist + emit a SignalWire transcription utterance event.

    Extracted from the ``/transcription`` webhook so the ``/sidecar/events``
    webhook can reuse the same handling — per the SignalWire dev's confirmation,
    ``calling.ai_sidecar`` is a strict superset of ``calling.live_transcribe``
    and emits the same utterance shape when ``live_events: true`` is in the
    start params. We want utterance events to flow through the same
    persist-then-socket-emit pipeline regardless of which webhook hears them
    first, so call history stays complete across coach attach/detach.

    Args:
        data: raw webhook JSON.
        source: 'transcription' or 'sidecar' — only used for logging so we
            can tell which webhook a given utterance arrived through.

    Returns:
        Tuple of (handled: bool, status_string). ``handled=False`` means this
        wasn't a recognizable utterance event and the caller should keep
        processing other event kinds (relevant for the sidecar webhook,
        which multiplexes utterances with coaching events).
    """
    # Extract call_id from the nested structure
    call_info = data.get('call_info', {})
    call_id = call_info.get('call_id')

    # Check if this is an utterance (transcript) event
    utterance = data.get('utterance', {})

    if not call_id:
        # Try channel_data as fallback
        channel_data = data.get('channel_data', {})
        call_id = channel_data.get('call_id')

    logger.info(f"[{source}] Extracted - Call ID: {call_id}, Has utterance: {bool(utterance)}")

    # Log the webhook event (use call.id if we can find it)
    call = Call.find_by_sid(call_id) if call_id else None
    WebhookEvent.log_event(
        event_type=f"{source}_utterance" if source != 'transcription' else "transcription",
        payload=data,
        call_id=call.id if call else None
    )

    # Check for recording URL in channel_data
    channel_data = data.get('channel_data', {})
    swml_vars = channel_data.get('SWMLVars', {})
    recording_url = swml_vars.get('record_call_url')

    # Update call with recording URL if present
    if call and recording_url and not call.recording_url:
        call.recording_url = recording_url
        db.session.commit()
        logger.info(f"Updated call {call_id} with recording URL: {recording_url}")

    if not (utterance and call_id):
        return (False, 'no-utterance')

    # Extract transcript data from utterance
    text = utterance.get('content', '')
    confidence = utterance.get('confidence', 0)
    role = utterance.get('role', 'unknown')
    language = utterance.get('lang', 'en-US')
    timestamp = utterance.get('timestamp', 0)

    # Check if transcription is final (not partial). With partial_events: False
    # in SWML we should only get final transcriptions, but check
    # ``final``/``is_final`` defensively just in case.
    is_final = utterance.get('final', utterance.get('is_final', True))

    # Skip partial transcriptions to avoid duplicates
    if not is_final:
        logger.debug(f"[{source}] Skipping partial transcription: '{text}'")
        return (True, 'skipped-partial')

    if not call:
        logger.warning(f"[{source}] Call not found for ID: {call_id}")
        return (True, 'no-call')

    # Get the next sequence number
    last_trans = db.session.query(Transcription).filter_by(
        call_id=call.id
    ).order_by(Transcription.sequence_number.desc()).first()
    sequence = (last_trans.sequence_number + 1) if last_trans else 0

    # Map role to speaker format expected by frontend
    speaker = 'caller' if role == 'remote-caller' else 'agent'

    # Save transcription
    transcription = Transcription(
        call_id=call.id,
        transcript=text,
        confidence=confidence,
        is_final=is_final,
        sequence_number=sequence,
        speaker=speaker,
        language=language
    )
    db.session.add(transcription)
    db.session.commit()

    logger.info(
        f"[{source}] Saved transcript: '{text}' "
        f"(confidence: {confidence}, role: {role}, speaker: {speaker})"
    )

    # Emit transcription to call room (all agents viewing this call have
    # joined this room)
    transcription_data = {
        'call_sid': call_id,
        'text': text,
        'confidence': confidence,
        'is_final': is_final,
        'sequence': sequence,
        'role': role,
        'speaker': speaker,  # 'caller' or 'agent' (mapped from role)
        'timestamp': timestamp
    }
    socketio.emit('transcription', transcription_data, room=call_id)
    logger.info(f"[{source}] ✓ Emitted transcription to call room {call_id}")

    # Agent Assist Factbook auto-mode (see AGENT_ASSIST.md). Best-effort;
    # failures must never break transcription persistence.
    if speaker == 'caller' and call.assigned_agent_id:
        try:
            _trigger_kb_auto_search_if_enabled(call, call_id)
        except Exception as e:
            logger.warning(f"[{source}] [Factbook auto] trigger failed: {e}")

    return (True, 'ok')


@webhooks_bp.route('/transcription', methods=['POST'])
@require_webhook_auth
def transcription():
    """Handle live transcription webhook from SignalWire.

    Persists and emits via ``_process_utterance_event``; same helper is reused
    by the sidecar webhook so utterances flow consistently regardless of
    which verb is currently transcribing the call.
    """
    try:
        data = request.get_json() if request.is_json else request.form.to_dict()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/transcription")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # End-of-session AI summary (live_transcribe ai_summary:true) arrives here
        # as a calling.ai.transcribe.conversation_log event carrying
        # conversation_summary — NOT an utterance. Route it to the wrap-up.
        summary_text = _extract_conversation_summary(data)
        if summary_text:
            _apply_ai_wrapup_summary(data, summary_text)
            return jsonify({'status': 'ok', 'handled': 'conversation_summary'}), 200

        _process_utterance_event(data, source='transcription')
        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        logger.error(f"Error processing transcription webhook: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/summary', methods=['POST'])
@require_webhook_auth
def summary():
    """Handle transcription summary webhook from SignalWire."""
    try:
        data = request.get_json() if request.is_json else request.form.to_dict()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/summary")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # Extract call_id from various possible locations
        call_id = data.get('call_id')
        if not call_id and 'call_info' in data:
            call_id = data['call_info'].get('call_id')
        if not call_id and 'channel_data' in data:
            call_id = data['channel_data'].get('call_id')

        # Extract summary text
        summary_text = None
        if 'conversation_summary' in data:
            summary_text = data['conversation_summary']
        elif 'summary' in data:
            if isinstance(data['summary'], str):
                summary_text = data['summary']
            elif isinstance(data['summary'], dict):
                summary_text = data['summary'].get('text', data['summary'].get('content'))
        elif 'ai_summary' in data:
            summary_text = data['ai_summary']

        logger.info(f"Extracted - Call ID: {call_id}, Summary: {summary_text[:100] if summary_text else None}")

        # Find the call and save summary
        if call_id and summary_text:
            logger.info(f"Looking up call with ID: {call_id}")
            call = Call.find_by_sid(call_id)  # Note: find_by_sid actually searches by call_id
            if call:
                # Save summary to call
                call.summary = summary_text
                db.session.commit()
                logger.info(f"✓ Saved summary for call {call_id} (DB ID: {call.id})")

                # Log the webhook event
                WebhookEvent.log_event(
                    event_type="summary_received",
                    payload=data,
                    call_id=call.id
                )

                # Emit summary to call-specific room only
                socketio.emit('summary', {
                    'call_sid': call_id,  # Frontend expects call_sid
                    'summary': summary_text
                }, room=call_id)
                logger.info(f"✓ Emitted summary to room: {call_id}")

                # Also emit to user room for UI updates
                socketio.emit('summary', {
                    'call_sid': call_id,  # Frontend expects call_sid
                    'summary': summary_text
                }, room=str(call.user_id))
                logger.info(f"✓ Emitted summary to user room: {call.user_id}")
            else:
                # SEC-04 fix (2026-06-02 audit): the previous code path here
                # FABRICATED a Call row attributed to the first/system user
                # whenever a summary arrived for an unknown call_id. Since
                # the payload (call_id + summary text) is attacker-
                # controllable (the soft-mode webhook fallback used to make
                # this unauth — fixed by SEC-01; but the attribution-
                # mismatch was still wrong even with auth on), an attacker
                # who knew the WEBHOOK_AUTH_REQUIRED creds could spray
                # fabricated calls into the system, attributed to the
                # first admin. We now refuse. A real call will have a Call
                # row created by /initial-call SWML BEFORE the summary
                # webhook fires; a missing row means either (a) the call
                # never reached our SWML (so it isn't ours), or (b) row
                # creation failed (which is a separate bug to investigate
                # — not "manufacture a fake row on the fly").
                logger.warning(
                    f"✗ Summary webhook received for unknown call_id={call_id}; "
                    f"refusing to fabricate a Call row. If this is a legitimate "
                    f"call, ensure /initial-call SWML created the Call before "
                    f"the AI session ended."
                )
                # Still log the event for audit trail — payload is preserved
                # but with call_id=None so it doesn't pretend to belong to a
                # call row we don't have.
                WebhookEvent.log_event(
                    event_type="summary_orphan",
                    payload=data,
                    call_id=None,
                )
        else:
            logger.warning(f"Missing call_id ({call_id}) or summary in webhook data")

        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        logger.error(f"Error processing summary webhook: {str(e)}")
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/queue-status', methods=['POST'])
@require_webhook_auth
def queue_status():
    """Receive lifecycle events from SignalWire's ``enter_queue`` verb.

    Bridge-mode parked callers are managed natively by SignalWire — we get
    these callbacks (``entering / connecting / connected / leaving / timeout /
    hangup / failed``) as the source of truth and mirror them into our Redis
    read-model so the dashboard's Queue tab keeps working. We also forward
    them as Socket.IO ``queue_update`` events for live UI refresh.

    Payload shape (from SWML enter_queue spec): includes ``queue_result``
    (the lifecycle state), ``queue_name``, ``entry_position``, ``entry_size``,
    ``wait_time``, plus the standard ``call_info``/``call_id`` envelope.

    Best-effort throughout — webhook ack with 200 even on internal errors so
    SignalWire doesn't retry-storm us; the failure surfaces in our logs.
    """
    try:
        data = request.get_json(silent=True) if request.is_json else None
        data = data or request.form.to_dict() or {}

        logger.info("=" * 50)
        logger.info("WEBHOOK: /api/webhooks/queue-status")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("=" * 50)

        # Defensive parsing — SignalWire's exact key naming hasn't been
        # nailed down across all SDK versions. Look in the most likely spots.
        result = (
            data.get('queue_result')
            or data.get('result')
            or data.get('state')
            or 'unknown'
        )
        queue_name = (
            data.get('queue_name')
            or data.get('queue')
            or 'unknown'
        )
        call_info = data.get('call_info') or {}
        call_id = (
            data.get('call_id')
            or call_info.get('call_id')
        )
        position = data.get('entry_position') or data.get('position')
        size = data.get('entry_size') or data.get('size')
        wait_time = data.get('wait_time') or data.get('waited_seconds')

        # Persist for audit.
        call = Call.find_by_sid(call_id) if call_id else None
        WebhookEvent.log_event(
            event_type=f"queue_status_{result}",
            payload=data,
            call_id=call.id if call else None,
        )

        # Per the SWML schema for enter_queue, queue_result fires once at
        # verb completion with one of: connected | timeout | hangup | failed.
        # There is no 'entering' lifecycle event — promotion from 'pending'
        # to 'waiting' happens via the call-state webhook on 'answered'
        # instead. This handler only sees terminal states.
        if call:
            try:
                if result == 'connected':
                    # Bridge succeeded: agent picked up. Status owned by the
                    # call-state flow from here on (will become 'active').
                    call.status = 'assigned'
                elif result in ('timeout', 'hangup', 'failed'):
                    # Caller never bridged to an agent — verb ended without
                    # success. Mark the row ended if no agent has taken it.
                    # Includes 'pending' so a hangup before call-state
                    # 'created' fires still closes the row. update_status
                    # stamps end_reason: 'timeout' → abandoned_in_queue
                    # explicitly; hangup/failed fall to compute_end_reason.
                    if call.status in ('pending', 'waiting', 'queued', 'assigned'):
                        hint = 'abandoned_in_queue' if result == 'timeout' else None
                        call.update_status('ended', end_reason=hint)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"queue-status: failed to persist Call row: {e}")

        # Emit Socket.IO for the dashboard. Frontend Queue tab listens for
        # `queue_update` with action='added'/'ended'/'updated'. 'added' is
        # emitted by the call-state webhook on promotion, not here.
        action_map = {
            'connected': 'updated',
            'hangup': 'ended',
            'timeout': 'ended',
            'failed': 'ended',
        }
        action = action_map.get(result, 'updated')
        try:
            socketio.emit('queue_update', {
                'call': call.to_dict() if call else {'signalwireCallSid': call_id},
                'queue_id': queue_name,
                'action': action,
                'position': position,
                'size': size,
                'wait_time': wait_time,
                'native_queue_event': result,
            })
            logger.info(
                f"queue-status: emit queue_update action={action} "
                f"queue={queue_name} call={call_id} pos={position}"
            )
        except Exception as e:
            logger.warning(f"queue-status: socketio emit failed: {e}")

        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Error in queue-status webhook: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # Return 200 anyway — SignalWire shouldn't retry on our bugs.
        return jsonify({'status': 'error', 'message': str(e)}), 200


@webhooks_bp.route('/sidecar/events', methods=['POST'])
@require_webhook_auth
def sidecar_events():
    """Consume `calling.ai.sidecar` events posted by the AI Coach sidecar.

    Per the dev's Q2 answer (2026-05-13), backend-subscribable via URL
    webhook. The sidecar verb (attached in ``app.services.coach``) configures
    this URL, and ``global_data`` rides on every event so we don't need a
    DB lookup to route the resulting Socket.IO emit.

    Expected event shapes (sidecar API is still firming up; defensive parsing
    here lets us absorb minor field renames without breaking the pipeline):
      - suggestion    — sidecar emitted a tip ("auto" mode default)
      - ask_answer    — response to an explicit agent-initiated ask (M10)
      - skip          — sidecar deliberately stayed silent (debug only)
      - tool_call     — sidecar invoked a SWAIG tool (M11 lookup_kb, etc.)

    Routing:
      - Emit ``coaching_suggestion`` to the call's call_sid room AND to the
        agent's user-id room. The call room covers anyone watching the call
        live (admin observers); the user room covers the agent themselves if
        they happen not to be looking at the call view.
      - For ``ask_answer``, also push to a Redis FIFO list keyed by call_sid
        so the M10 ask-correlation shim can pop matches when the agent's UI
        polls for their answer.
    """
    try:
        data = request.get_json() if request.is_json else request.form.to_dict()

        logger.info("=" * 50)
        logger.info("WEBHOOK: /api/webhooks/sidecar/events")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("=" * 50)

        # Coach events are WRAPPED (verified call 5a1def2d, 2026-06-08):
        #   {"call_info": {...}, "sidecar_event": {"type": "...", ...}}
        # Transcription utterances use a different envelope ({"utterance":...,
        # "confidence":...}) handled by the fast-path below. call_info.call_id
        # is present on every shape, so route off that.
        call_info = data.get('call_info') or {}
        sidecar = data.get('sidecar_event')
        if not isinstance(sidecar, dict):
            sidecar = {}
        # global_data only rides start/stop/final events; per-turn events carry
        # just channel_data. Pull call_sid from wherever it's available.
        global_data = sidecar.get('global_data') or data.get('global_data') or {}
        call_sid = (
            global_data.get('call_sid')
            or call_info.get('call_id')
            or (sidecar.get('channel_data') or {}).get('call_id')
            or data.get('call_id')
        )

        # Persist for audit/replay regardless of whether we can route it —
        # missing global_data shouldn't lose the event.
        call = Call.find_by_sid(call_sid) if call_sid else None
        WebhookEvent.log_event(
            event_type="sidecar_event",
            payload=data,
            call_id=call.id if call else None,
        )

        # Utterance fast-path. Per the SignalWire dev (2026-05-26),
        # ``calling.ai_sidecar`` is built on top of ``calling.live_transcribe``
        # and emits the same utterance event shape when ``live_events: true``
        # is in the start params. When the sidecar is the active transcriber
        # (we swap them on coach attach to satisfy the "one transcriber per
        # leg" SignalWire constraint), utterance events arrive HERE instead
        # of at /api/webhooks/transcription. Forward to the shared helper so
        # the live transcription panel and call history stay populated while
        # coach is on.
        if data.get('utterance'):
            handled, _status = _process_utterance_event(data, source='sidecar')
            if handled:
                return jsonify({'status': 'ok', 'kind': 'utterance'}), 200

        # The coach-event discriminator is ``sidecar_event.type`` (NOT a
        # top-level field). Verified types (call 5a1def2d): start, turn,
        # request, tool_call, tool_result, skip, insight, ask_answer, stop,
        # final.
        kind = sidecar.get('type') or 'unknown'

        # Only agent-facing kinds surface in the UI: ``insight`` (coaching
        # advice the model produced) and ``ask_answer`` (reply to an explicit
        # /coach/ask). Everything else — turn/skip/tool_call/tool_result/
        # request/start/stop/final — is observability: already persisted
        # above, never pushed to the agent. (Transcription is handled by the
        # utterance fast-path, NOT ``turn``.)
        if kind not in ('insight', 'ask_answer'):
            return jsonify({'status': 'ok', 'kind': kind}), 200

        # Routing context. agent_user_id/queue_slug only ride global_data
        # (start/stop/final events); per-turn insight events don't carry it,
        # so fall back to the call's assigned agent for the user-room emit.
        agent_user_id = global_data.get('agent_user_id') or (
            call.assigned_agent_id if call else None
        )
        queue_slug = global_data.get('queue_slug') or (
            call.queue_id if call else None
        )
        coach_mode = global_data.get('coach_mode')
        coach_intensity = global_data.get('coach_intensity')

        # Advice text. Per the spec ``insight`` carries it under ``raw``; be
        # defensive about str vs structured, and keep ask_answer slots. Exact
        # insight field layout still to be confirmed from a live insight
        # sample (this call produced only skips) — every event is persisted to
        # webhook_events for verification.
        raw_field = sidecar.get('raw')
        suggestion_text = (
            (raw_field if isinstance(raw_field, str) else None)
            or (raw_field.get('response') if isinstance(raw_field, dict) else None)
            or (raw_field.get('text') if isinstance(raw_field, dict) else None)
            or (raw_field.get('content') if isinstance(raw_field, dict) else None)
            or sidecar.get('insight')
            or sidecar.get('suggestion')
            or sidecar.get('answer')
            or sidecar.get('text')
            or sidecar.get('message')
            or (sidecar.get('content') if isinstance(sidecar.get('content'), str) else None)
            or ''
        )

        # ask correlation token (M10) — may not exist until SignalWire ships
        # the ask_id field. Until then we fall back to a FIFO pop against
        # the per-call pending_asks list that coach_ask pushes to.
        ask_id = sidecar.get('ask_id') or sidecar.get('correlation_id')
        matched_question: Optional[str] = None
        if kind == 'ask_answer' and not ask_id and call_sid:
            try:
                from app.services.redis_service import get_redis_client
                r = get_redis_client()
                if r is not None:
                    key = f"coach_pending_asks:{call_sid}"
                    raw = r.lpop(key)
                    if raw:
                        try:
                            pending = json.loads(raw)
                            ask_id = pending.get('ask_id')
                            matched_question = pending.get('question')
                        except (TypeError, ValueError):
                            # Corrupt FIFO entry — just drop it and move on.
                            pass
            except Exception as e:
                logger.warning(
                    f"Sidecar ask FIFO correlation failed (non-fatal): {e}"
                )

        emit_payload = {
            'call_sid': call_sid,
            'agent_user_id': agent_user_id,
            'queue_slug': queue_slug,
            'coach_mode': coach_mode,
            'coach_intensity': coach_intensity,
            'kind': kind,
            'text': suggestion_text,
            'ask_id': ask_id,
            # Echo the matched agent question back when FIFO correlated, so the
            # UI can pair it with the agent's optimistic "You asked" bubble.
            'matched_question': matched_question,
            'timestamp': sidecar.get('ts') or data.get('timestamp'),
            # Raw kept on the payload for debugging — the frontend ignores it
            # unless the developer console is open.
            'raw': data,
        }

        if call_sid:
            socketio.emit('coaching_suggestion', emit_payload, room=call_sid)
            logger.info(
                f"✓ Emitted coaching_suggestion ({kind}) to call room {call_sid}"
            )
        if agent_user_id:
            socketio.emit('coaching_suggestion', emit_payload, room=str(agent_user_id))
            logger.info(
                f"✓ Emitted coaching_suggestion ({kind}) to agent room {agent_user_id}"
            )
        if not call_sid and not agent_user_id:
            logger.warning(
                "Sidecar event arrived with no call_sid or agent_user_id in "
                "global_data; logged but not emitted."
            )

        # ask_answer FIFO for M10 correlation — only push if we have the
        # call_sid (no point queuing un-routable answers). Best-effort: a
        # Redis hiccup mustn't block the webhook from acknowledging.
        if kind in ('ask_answer', 'ask') and call_sid:
            try:
                from app.services.redis_service import get_redis_client
                r = get_redis_client()
                if r is not None:
                    key = f"coach_ask_answers:{call_sid}"
                    r.rpush(key, json.dumps(emit_payload))
                    # Cap at 50 entries to avoid runaway growth on long calls,
                    # and expire 1h after the last push so finished calls
                    # don't leak keys indefinitely.
                    r.ltrim(key, -50, -1)
                    r.expire(key, 3600)
            except Exception as e:
                logger.warning(
                    f"Failed to push sidecar ask_answer to Redis FIFO "
                    f"(non-fatal): {e}"
                )

        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Error processing sidecar events webhook: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Return 200 anyway — SignalWire retries on 5xx and we don't want a
        # bug in our parser to cause repeated re-deliveries.
        return jsonify({'status': 'error', 'message': str(e)}), 200


@webhooks_bp.route('/coach/lookup_kb', methods=['POST'])
@require_webhook_auth
def coach_lookup_kb():
    """SWAIG tool endpoint — the AI Coach sidecar calls this to fetch KB facts.

    Defined as a SWAIG ``function`` in :func:`app.services.coach.build_sidecar_start_params`,
    pointed at this URL via ``web_hook_url``. The sidecar invokes it when its
    prompt determines that a knowledge-base lookup would help the agent.

    Input shape (SWAIG ``function_call`` payload — keys vary slightly across
    SignalWire releases; we read defensively):
      {
        "function": "lookup_kb",
        "argument": {"parsed": [{"query": "...", "top_k": 3}]}
                or "arguments": {"query": "...", "top_k": 3}
                or top-level "query"/"top_k"
        "global_data": {agent_user_id, queue_slug, ...}  // from coach.py
      }

    Returns SWAIG response: ``{response: "<text fed back to the sidecar>"}``.
    The sidecar then folds the response into its next suggestion.
    """
    import requests as http_requests

    try:
        data = request.get_json(silent=True) or {}
        logger.info("=" * 50)
        logger.info("WEBHOOK: /api/webhooks/coach/lookup_kb")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("=" * 50)

        # SWAIG payload shape isn't 100% stable across SignalWire releases.
        # Try the documented "argument.parsed[0]" path first, then a flat
        # "arguments" dict, then top-level keys as last resort.
        args = {}
        argument = data.get('argument') or {}
        parsed = argument.get('parsed') if isinstance(argument, dict) else None
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            args = parsed[0]
        elif isinstance(data.get('arguments'), dict):
            args = data['arguments']
        elif isinstance(argument, dict) and 'query' in argument:
            args = argument
        else:
            args = {'query': data.get('query'), 'top_k': data.get('top_k')}

        query = (args.get('query') or '').strip()
        top_k = args.get('top_k') or 3
        if not isinstance(top_k, int) or top_k < 1 or top_k > 10:
            top_k = 3

        if not query:
            return jsonify({
                'response': 'No query provided — ask the agent for clarification.',
            }), 200

        # Choose the collection. For now, defaults to sales_knowledge — same
        # placeholder as the Factbook auto-search. Once per-queue collection
        # assignments land (roadmap), derive from global_data.queue_slug.
        global_data = data.get('global_data') or {}
        queue_slug = global_data.get('queue_slug')  # noqa: F841 — TODO use this
        collection_name = 'sales_knowledge'

        ai_agents_url = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')
        try:
            resp = http_requests.post(
                f"{ai_agents_url}/search",
                json={
                    'collection_name': collection_name,
                    'query': query,
                    'top_k': top_k,
                },
                timeout=10,
            )
        except http_requests.RequestException as exc:
            logger.warning(f"[Coach lookup_kb] /search request failed: {exc}")
            return jsonify({
                'response': 'Knowledge base is temporarily unavailable.',
            }), 200

        if not resp.ok:
            return jsonify({
                'response': 'Knowledge base returned an error — skipping this lookup.',
            }), 200

        payload = resp.json()
        results = payload.get('results') or []
        if not results:
            return jsonify({
                'response': f'No matching facts in the knowledge base for "{query}".',
            }), 200

        # Compose a concise summary back to the sidecar. Keep it short — the
        # sidecar's own prompt budget is finite and the agent only sees the
        # sidecar's distilled suggestion, not these raw chunks.
        lines = []
        for r in results[:top_k]:
            content = (r.get('content') or '').strip().replace('\n', ' ')
            if len(content) > 220:
                content = content[:220].rsplit(' ', 1)[0] + '…'
            source = r.get('filename') or r.get('section') or 'unknown'
            lines.append(f"- [{source}] {content}")
        body = (
            f"Top {len(lines)} knowledge-base facts for \"{query}\":\n"
            + "\n".join(lines)
        )

        return jsonify({'response': body}), 200
    except Exception as e:
        logger.error(f"Error in coach lookup_kb: {e}")
        # Return a soft error to the sidecar so it doesn't crash the suggestion.
        return jsonify({
            'response': 'Knowledge base lookup encountered an internal error.',
        }), 200


@webhooks_bp.route('/post-prompt', methods=['POST'])
@require_webhook_auth
def post_prompt():
    """
    Handle post_prompt webhook from SignalWire AI agents.
    This receives structured data after a call ends including:
    - post_prompt_data: The AI's analysis/summary of the conversation
    - global_data: Data collected during the call
    - caller info: phone number, name, etc.
    """
    try:
        data = request.get_json() if request.is_json else request.form.to_dict()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/post-prompt")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # Persist the raw payload for offline debugging (most-recent + history).
        # captures/postprompt-latest.json + captures/postprompt.jsonl
        capture_webhook_payload('postprompt', data)

        # Extract key fields from the post_prompt payload
        app_name = data.get('app_name')
        call_id = data.get('call_id')
        ai_session_id = data.get('ai_session_id')
        caller_id_name = data.get('caller_id_name')
        caller_id_num = data.get('caller_id_num')

        # Post prompt data (the AI's structured response)
        post_prompt_data = data.get('post_prompt_data', {})
        raw_summary = post_prompt_data.get('raw')
        parsed_summary = post_prompt_data.get('parsed', [])

        # Global data collected during the call
        global_data = data.get('global_data', {})

        logger.info(f"Post-prompt received from {app_name} for call {call_id}")
        logger.info(f"Caller: {caller_id_name} ({caller_id_num})")
        logger.info(f"Global data keys: {list(global_data.keys())}")

        # Find the call in database
        call = Call.find_by_sid(call_id) if call_id else None

        if call:
            # Update call with post_prompt data
            # Merge global_data into ai_context
            existing_context = json.loads(call.ai_context) if call.ai_context else {}
            merged_context = {**existing_context, **global_data}

            # Add parsed summary if available. ai_assessment is initialized
            # OUTSIDE the block so the unconditional `if ai_assessment:` outcome
            # check further down can never raise NameError on an empty parsed array.
            ai_assessment = {}
            if parsed_summary and len(parsed_summary) > 0:
                merged_context['parsed_summary'] = parsed_summary[0]

                # Tier B v1: AI-emitted disposition + post_mortem auto-fill the
                # wrap-up fields so the panel shows them pre-populated. Human
                # agents can still edit. We only set when empty so a manual
                # value (or an earlier AI pass) is never clobbered.
                ai_assessment = parsed_summary[0] if isinstance(parsed_summary[0], dict) else {}
                ai_filled_wrapup = False
                raw_disp = (ai_assessment.get('disposition') or '').strip()
                if raw_disp and not call.disposition_code:
                    # Validate against the canonical list — defensive; the LLM
                    # was told to pick from that list but might hallucinate.
                    from app.api.calls import DISPOSITION_CODE_SET
                    if raw_disp in DISPOSITION_CODE_SET:
                        call.disposition_code = raw_disp
                        ai_filled_wrapup = True
                        logger.info(
                            f"post_prompt: AI disposition auto-set call {call.id} -> {raw_disp!r}"
                        )
                    else:
                        logger.warning(
                            f"post_prompt: AI emitted unknown disposition {raw_disp!r} "
                            f"for call {call.id}; ignoring"
                        )
                post_mortem = (ai_assessment.get('post_mortem') or '').strip()
                if post_mortem and not call.agent_notes:
                    call.agent_notes = post_mortem
                    ai_filled_wrapup = True
                    logger.info(
                        f"post_prompt: AI post_mortem auto-filled into agent_notes "
                        f"for call {call.id} ({len(post_mortem)} chars)"
                    )
                # Explicit provenance for the "Captured by AI" badge — stamped
                # only when we actually auto-filled this pass and a human hasn't
                # already claimed the wrap-up.
                if ai_filled_wrapup and not call.wrap_up_source:
                    call.wrap_up_source = 'ai'

            call.ai_context = json.dumps(merged_context)

            # If we have a raw summary and no existing summary, use it
            if raw_summary and not call.summary:
                call.summary = raw_summary

            # RE-AUDIT-05 fix (2026-06-03): the previous code REMOVED the
            # call from the queue zset here, then immediately below set
            # status='waiting'. Result: a call ready for human pickup
            # was status='waiting' but invisible to push-dispatch (which
            # reads the zset to find candidates when an agent goes
            # Available). Only manual Take worked, because that path
            # looks up by Call.id not the zset. AI-01 activating the
            # post_prompt URL exposed this latent bug — before AI-01
            # the URL wasn't set so post_prompt never fired, and the
            # dead code never broke push-dispatch.
            #
            # The remove was nonsensical to begin with: the call SHOULD
            # be in the queue at this point, because the caller is
            # still on the line waiting for a human. Deleted entirely.
            # The original-intent comment "Clean up any queue entries"
            # doesn't match what /post-prompt actually fires for — it's
            # an AI-session-ended hook, not a call-end hook.

            # post_prompt fires when the AI conversation ends, NOT necessarily
            # when the phone call ends — but for inbound relay_script calls the
            # call-status webhook NEVER fires (call_state_url is not honored
            # for relay_script handlers; see call_watchdog docstring), so
            # whatever we set here is the call's final state until the
            # 35-minute watchdog cap. Route on the session outcome,
            # corroborated by hard state:
            #   - handed to another AI agent → leave status alone; that
            #     session posts its own post_prompt when IT ends
            #   - handed to a human queue → 'waiting' so push-dispatch and
            #     manual Take can pick it up
            #   - anything else (abandoned/resolved/hangup) with no assigned
            #     agent and no live conference → the call is over; close it
            #     NOW instead of letting it haunt the dashboard as a phantom
            #     queue entry until the watchdog cap.
            outcome = ''
            if ai_assessment:
                outcome = str(ai_assessment.get('outcome') or '').lower()

            if call.status in ('answered', 'ai_active', 'waiting'):
                if 'transferred_to_ai' in outcome:
                    logger.info(
                        f"Call {call_id} AI session ended via AI->AI handoff — "
                        f"status untouched ({call.status})"
                    )
                elif 'human' in outcome:
                    call.status = 'waiting'
                    logger.info(
                        f"Call {call_id} AI session ended — set to 'waiting' "
                        f"for human pickup"
                    )
                elif not call.assigned_agent_id and not call.conference_name:
                    call.update_status('ended')
                    logger.info(
                        f"Call {call_id} AI session ended (outcome={outcome!r}, "
                        f"no agent/conference) — call closed"
                    )

            db.session.commit()
            logger.info(f"✓ Updated call {call.id} with post_prompt data (status: {call.status})")

            # Log the webhook event
            WebhookEvent.log_event(
                event_type="post_prompt_received",
                payload=data,
                call_id=call.id
            )

            # Emit call_update so frontend sees the status change
            from app.services.callcenter_socketio import emit_call_update
            emit_call_update(call)

            # Only emit call_ended if the call is actually ended/completed
            # If status is 'waiting', the call is still active and should stay in the queue
            if call.status in ('ended', 'completed'):
                call_ended_data = {
                    'callId': call.id,
                    'call_sid': call_id,
                    'reset_ui': True
                }
                if call.user_id:
                    socketio.emit('call_ended', call_ended_data, room=str(call.user_id))
                socketio.emit('call_ended', call_ended_data)

            # Also emit to call room
            socketio.emit('post_prompt_received', {
                'call_id': call.id,
                'call_sid': call_id,
                'ai_context': merged_context,
                'summary': call.summary
            }, room=call_id)

        else:
            logger.warning(f"Call not found for post_prompt: {call_id}")
            # Log anyway for debugging
            WebhookEvent.log_event(
                event_type="post_prompt_orphaned",
                payload=data,
                call_id=None
            )

        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        logger.error(f"Error processing post_prompt webhook: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/debug-events', methods=['POST'])
@require_webhook_auth
def debug_events():
    """Capture SWAIG debug webhook events to file for offline diagnosis.

    The agents only point SignalWire at this endpoint when
    DEBUG_WEBHOOK_ENABLED=true (see ai-agents/main_agent.py:capture_base_url).
    At debug_webhook_level=2 the platform POSTs many events per call (LLM
    request/response, step/context changes, fillers, conversation_add), so we
    deliberately keep this light: stream each event to
    captures/debug-events.jsonl and skip all DB work. Inspect the file after a
    test call to see exactly what the agent did, turn by turn — e.g. a burst of
    step_change/filler events is the "skips to the final step + spams fillers"
    symptom, and the llm_request/llm_response pair shows why the model jumped.
    """
    try:
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict() if request.form else {}
        event_type = data.get('label') or data.get('action') or 'unknown'
        call_id = data.get('call_id')
        # Stream only — the "latest single event" isn't useful; the sequence is.
        capture_webhook_payload('debug-events', data, latest=False)
        logger.info(f"DEBUG EVENT [{event_type}] call={call_id}")
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Error processing debug webhook: {e}")
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/recording', methods=['POST'])
@require_webhook_auth
def recording():
    """Handle recording webhook from SignalWire."""
    try:
        data = request.form.to_dict() if request.form else request.get_json()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/recording")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # Extract from nested params if present (SWML format)
        if 'params' in data:
            params = data['params']
            call_id = params.get('call_id')
            recording_url = params.get('url')
            recording_sid = params.get('recording_id')
        else:
            # Old format or Twilio SDK format
            call_id = data.get('CallSid') or data.get('call_sid') or data.get('call_id')
            recording_url = data.get('RecordingUrl') or data.get('recording_url')
            recording_sid = data.get('RecordingSid') or data.get('recording_sid')

        logger.info(f"Extracted - Call ID: {call_id}, Recording URL: {recording_url}")

        # Look up the Call row so we can persist the URL AND log the event
        # against the right DB id. Without this we lose two things:
        #   1. recording_url never lands on the Call row — so when the agent
        #      opens the call-detail tab moments after hangup, the
        #      ``interactions`` API returns recordingUrl=null and the player
        #      doesn't appear until the user refreshes (the bug the user
        #      hit). The other recording extraction path (transcription
        #      webhook, swml_vars.record_call_url) was the only thing
        #      writing the column before — and it doesn't always fire on
        #      conference / human-handled calls.
        #   2. ``WebhookEvent.log_event(call_id=...)`` was being passed the
        #      raw SignalWire call_sid, which is the wrong type for the
        #      ``calls.id`` foreign key — the row was either rejected or
        #      stored against a bogus parent. Look up the DB id properly.
        call = Call.find_by_sid(call_id) if call_id else None
        WebhookEvent.log_event(
            event_type="recording_completed",
            payload=data,
            call_id=call.id if call else None,
        )

        # Persist recording_url to the Call row (idempotent — only write
        # when we actually got one back AND the column is empty).
        if call and recording_url and not call.recording_url:
            call.recording_url = recording_url
            db.session.commit()
            logger.info(
                f"Updated call {call.id} (sid={call_id}) with recording URL: {recording_url}"
            )

        # Emit two events so any UI surface can update without a refresh:
        #   1. ``recording`` — the call's transcription/recording room
        #      (LiveCallTab subscribes here while the call is live).
        #   2. ``call_update`` — broadcast on Call.to_dict() so the
        #      contact-detail tab (which renders ``interaction.recordingUrl``
        #      from the interactions list) picks up the new URL on the
        #      next render of the call-detail panel.
        if recording_url:
            socketio.emit('recording', {
                'call_sid': call_id,  # Frontend expects call_sid
                'recording_url': recording_url,
                'recording_sid': recording_sid
            }, room=call_id)
            if call:
                # Lazy import to mirror the other call sites in this file
                # (avoids a top-level import cycle through the socketio
                # service).
                from app.services.callcenter_socketio import emit_call_update
                emit_call_update(call)

        return '', 200

    except Exception as e:
        logger.error(f"Error processing recording webhook: {str(e)}")
        return '', 500


@webhooks_bp.route('/recording-status', methods=['POST'])
@require_webhook_auth
def recording_status():
    """Handle recording status webhook from SignalWire."""
    try:
        data = request.form.to_dict() if request.form else request.get_json()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/recording-status")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        call_sid = data.get('CallSid') or data.get('call_sid')
        status = data.get('RecordingStatus') or data.get('status')

        logger.info(f"Extracted - Call SID: {call_sid}, Status: {status}")

        # Log the webhook event
        WebhookEvent.log_event(
            event_type=f"recording_{status}",
            payload=data,
            call_id=call_sid
        )

        # Emit recording status via WebSocket
        socketio.emit('recording_status', {
            'call_sid': call_sid,
            'status': status
        }, room=call_sid)

        return '', 200

    except Exception as e:
        logger.error(f"Error processing recording status webhook: {str(e)}")
        return '', 500


