from flask import request, jsonify
from app import db, socketio
from app.api import webhooks_bp
from app.models import Call, CallLeg, Transcription, WebhookEvent
from app.models.user import User
from app.services.knowledge import DEFAULT_KB_COLLECTION, kb_collection_for_queue
from app.services.redis_service import publish_event
from app.utils.request_logging import (
    mask_phone,
    request_summary,
    scrub_embedded_credentials,
)
from app.utils.webhook_auth import internal_service_auth, require_webhook_auth
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
    """Write a webhook payload to captures/ for offline debugging.

    Hard-off in hosted tenancy mode (§8.3): these files aggregate every
    tenant's raw payloads (transcripts, numbers, global_data) on shared
    disk and survive every reset — a cross-tenant data pool no debug
    convenience justifies on the public instance.
    """
    from app.utils.demo_config import tenancy_mode_active
    if tenancy_mode_active():
        return
    try:
        record = {
            'captured_at': datetime.utcnow().isoformat() + 'Z',
            # Scrubbed like the DB copy (A-1): SignalWire echoes our signed
            # callback URLs back in post-prompt payloads, and the embedded
            # credential has no debug value — the URL shape does.
            'payload': scrub_embedded_credentials(payload),
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
        # 'answered' mislabels human-answered calls as ai_active here; AI
        # handoffs set ai_active explicitly (swml.py, ai_control.py), and
        # Call.handler_type is the real AI-vs-human source of truth. Kept
        # until the dashboard status vocabulary grows a human_active state.
        'answered': 'ai_active',
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
            auth=internal_service_auth(),
            json={
                'collection_name': kb_collection_for_queue(
                    call.queue_id, call.workspace_id),
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


# ---------------------------------------------------------------------------
# SWAIG tool-call telemetry -> Event Stream (`ai_tool_call`)
#
# The platform reports every AI tool invocation in ONE envelope shape, and it
# reaches us from two places:
#   - /post-prompt  -> ``data['swaig_log']``, the whole session's tool calls.
#     ALWAYS ON: the agents set post_prompt_url unconditionally in their
#     dynamic-config callback (ai-agents/main_agent.py:918), so this is the
#     path that works on a clone-and-own install with nothing switched on.
#   - /debug-events -> ``data['swaig_call']``, live as each tool fires, but
#     only while an operator has DEBUG_WEBHOOK_ENABLED=true.
# Same fields either way, so they share one parser. Both shapes were verified
# against captured live payloads (backend/captures/postprompt.jsonl and
# debug-events.jsonl) before this was written.
#
# Deliberately NOT gated on demo mode — a cloner's Event Stream needs this
# exactly as much as the hosted demo does.
#
# Coverage note: agent-local tools (``search_knowledge`` via
# native_vector_search, and the MCP-gateway skills) execute inside the agents
# container and never touch the backend during a call, so the swaig_log
# backfill is the only place they show up at all. That is a property of where
# those tools run, not a gap in this parser.
# ---------------------------------------------------------------------------

def _swaig_tool_call_fields(entry):
    """Pull ``(function_name, arguments, call_db_id, call_sid)`` out of a SWAIG
    invocation envelope.

    Live shape, trimmed to the fields we read::

        {"command_name": "transfer_to_human",
         "command_arg": "{\"department\": \"support\"}",
         "post_data": {"function": "transfer_to_human",
                       "argument": {"parsed": [{"department": "support"}],
                                    "raw": "{\"department\": \"support\"}"},
                       "call_id": "<signalwire sid>",
                       "global_data": {"call_db_id": "132"},
                       "meta_data": {"call_db_id": "132"}}}

    Defensive on every field, for the same reason /queue-status and
    /sidecar/events are: SWAIG payload naming has moved across releases.
    """
    if not isinstance(entry, dict):
        return None, {}, None, None

    post_data = entry.get('post_data')
    if not isinstance(post_data, dict):
        post_data = {}

    function_name = entry.get('command_name') or post_data.get('function')

    # Arguments: prefer the platform's own parsed form, fall back to the raw
    # JSON string it echoes next to it.
    arguments = {}
    argument = post_data.get('argument')
    parsed = argument.get('parsed') if isinstance(argument, dict) else None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        arguments = parsed[0]
    else:
        raw = argument.get('raw') if isinstance(argument, dict) else None
        raw = raw or entry.get('command_arg')
        if isinstance(raw, dict):
            arguments = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                decoded = json.loads(raw)
            except (ValueError, TypeError):
                # Keep the unparseable text rather than silently dropping the
                # arguments — a garbled arg list is itself worth seeing.
                decoded = None
            arguments = decoded if isinstance(decoded, dict) else {'raw': raw}

    call_db_id = None
    for source in (post_data.get('global_data'), post_data.get('meta_data')):
        if isinstance(source, dict) and source.get('call_db_id'):
            call_db_id = source['call_db_id']
            break

    return function_name, arguments, call_db_id, post_data.get('call_id')


# Cross-path dedupe window. With debug telemetry on, ONE invocation reaches us
# twice: live from /debug-events mid-call, then again in the /post-prompt
# swaig_log backfill at hangup. The window has to span a whole call, and
# call_watchdog tolerates an 'active' call for 4h, so 6h covers it with room.
_TOOL_EMIT_DEDUPE_TTL_SECONDS = 6 * 60 * 60


def _tool_call_already_emitted(call_ref, function_name, epoch_time) -> bool:
    """True when another webhook path already emitted this exact invocation.

    Both envelopes carry the platform's own ``epoch_time`` for the invocation,
    so ``(call, function, epoch_time)`` identifies it across paths without us
    inventing an identity — and it stays distinct when the model calls the same
    function twice in one session. The key is claimed with ``SET NX``, so
    whichever path arrives first emits and the other stands down; no ordering
    assumption between the two webhooks.

    Fails OPEN — emit — when there's no Redis or no ``epoch_time`` to key on. A
    duplicated row in the panel is cosmetic; a silently dropped tool call makes
    the Event Stream look broken, which is the bug this whole path fixes.
    """
    if not epoch_time:
        return False
    try:
        from app.services.redis_service import get_redis_client
        redis_client = get_redis_client()
        if redis_client is None:
            return False
        key = f'aitool_emit:{call_ref}:{function_name}:{epoch_time}'
        claimed = redis_client.set(
            key, '1', nx=True, ex=_TOOL_EMIT_DEDUPE_TTL_SECONDS,
        )
        return not claimed
    except Exception as e:
        logger.debug(f"ai_tool_call dedupe check failed (emitting anyway): {e}")
        return False


def _emit_swaig_tool_call(entry, *, source, call=None):
    """Emit one ``ai_tool_call`` Event Stream event for a SWAIG invocation.

    Pass ``call`` when the caller already resolved the row (/post-prompt has
    it) to skip the sid lookup. Returns True when an event went out.

    Deduped across sources (see :func:`_tool_call_already_emitted`) so turning
    ``DEBUG_WEBHOOK_ENABLED`` on doesn't render every tool twice — the live
    /debug-events event and the /post-prompt backfill describe the same
    invocation, and with debug on the demo BOTH arrive.

    Best-effort throughout: this is telemetry for a demo panel and must never
    be able to fail the webhook that carries it.
    """
    # The SDK's Contexts/Steps navigation tools ride this same log flagged
    # `native: true` and carry no post_data — `next_step` and
    # `change_context`. They are workflow plumbing, not a tool the AI
    # "used", and emitting them buries the real calls in the panel (a
    # 5-tool session logs 8 entries, 3 of them navigation). Verified across
    # every captured session: native:true is exactly those two, and every
    # real tool call carries post_data instead.
    if isinstance(entry, dict) and entry.get('native') is True:
        return False

    try:
        function_name, arguments, call_db_id, payload_sid = (
            _swaig_tool_call_fields(entry)
        )
        if not function_name:
            return False

        # Prefer the resolved row's ids — those are what the dashboards key on.
        if call is None and payload_sid:
            call = Call.find_by_sid(payload_sid)
        call_id = call.id if call is not None else call_db_id
        call_sid = call.signalwire_call_sid if call is not None else payload_sid
        if call_id is None and call_sid is None:
            return False

        # Keyed on the sid in preference to the DB id: /debug-events can fire
        # before the Call row resolves, and the sid is the one identifier both
        # paths always agree on for the same call.
        if _tool_call_already_emitted(
            call_sid or call_id, function_name, entry.get('epoch_time'),
        ):
            logger.debug(
                f"ai_tool_call {function_name} for call {call_sid or call_id} "
                f"already emitted by another source — skipping ({source})"
            )
            return False

        # Persist BEFORE emitting. A live socket event is only observable
        # while someone has the call open, and the always-on producer
        # (/post-prompt) fires at the end of the AI session — for an AI-only
        # call that is the moment the desktop tears the panel down. Without a
        # row, the default-path telemetry would exist solely as a packet
        # nobody was mounted to receive. Best-effort: never fail the webhook.
        # Gated on the RESOLVED row, not on `call_id` — the fallback id comes
        # from the model's own global_data and is neither guaranteed to be an
        # integer nor to name a live row. WebhookEvent.call_id is an FK and
        # log_event commits, so a bad value would raise at flush and leave the
        # session poisoned for the rest of post_prompt. Losing a telemetry row
        # for an unresolvable call is the cheaper failure.
        if call is not None:
            try:
                WebhookEvent.log_event(
                    event_type='ai_tool_call',
                    payload={
                        'function_name': function_name,
                        'arguments': arguments,
                        'source': source,
                        'call_sid': call_sid,
                        # The platform's own invocation clock — the same field
                        # the cross-source dedupe keys on, so a persisted row
                        # can be reconciled against a live event.
                        'epoch_time': entry.get('epoch_time'),
                    },
                    call_id=call.id,
                )
            except Exception as e:
                # Roll back explicitly: log_event commits, so a failure here
                # leaves the session in a failed state and every later write
                # in this handler would raise on it.
                try:
                    db.session.rollback()
                except Exception:
                    pass
                logger.warning(
                    f"ai_tool_call persist failed ({source}, non-fatal): {e}"
                )

        from app.services.callcenter_socketio import emit_ai_tool_call
        emit_ai_tool_call(
            call_id,
            function_name,
            arguments=arguments,
            call_sid=call_sid,
            source=source,
        )
        return True
    except Exception as e:
        logger.warning(f"ai_tool_call emit failed ({source}, non-fatal): {e}")
        return False


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
        logger.info("Payload shape: %s", request_summary(request, data))
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
            logger.info(
                "Extracted params-format call=%s status=%s from=%s",
                call_id,
                status,
                mask_phone(from_number),
            )
        elif 'call' in data:
            # Alternative SignalWire format with call object
            call_data = data.get('call', {})
            call_id = call_data.get('call_id')
            status = call_data.get('call_state')
            from_number = call_data.get('from', call_data.get('from_number'))

            # Log for debugging
            logger.info(
                "Extracted call-format call=%s status=%s from=%s",
                call_id,
                status,
                mask_phone(from_number),
            )
        else:
            # Old format or Twilio SDK format
            call_id = data.get('CallSid') or data.get('call_sid') or data.get('call_id')
            status = data.get('CallStatus') or data.get('CallState') or data.get('status') or data.get('state')
            from_number = data.get('From') or data.get('from') or data.get('from_number')

        logger.info(
            "Call status webhook: %s - %s - from=%s",
            call_id,
            status,
            mask_phone(from_number),
        )

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
                logger.info(
                    "Updated call %s with from_number=%s",
                    call_id,
                    mask_phone(from_number),
                )

            db.session.commit()

            # On the first promotion from pending, tell the Queue tab to
            # add this call. Queue-status webhook ('entering') would also do
            # this — emit-or-update via the action='added' handler is
            # idempotent (dedups by call.id) so a double-fire is harmless.
            if was_pending and call.status == 'waiting':
                try:
                    from app.services.ws_rooms import workspace_room
                    socketio.emit('queue_update', {
                        'call': call.to_dict(include_contact=True),
                        'queue_id': call.queue_id,
                        'action': 'added',
                    }, room=workspace_room(call.workspace_id))
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

            # Emit call_update for Agent Dashboard — to the call's workspace
            # room (§8.1, replaces the all-sockets AI-active broadcast).
            # Everyone on that floor sees AI-active calls and can take over;
            # other workspaces never receive them. In clone-and-own the
            # single ws:1 room is the same floor-wide reach as before.
            from app.services.ws_rooms import workspace_room
            ws_room = workspace_room(call.workspace_id)
            socketio.emit('call_update', {'call': call_data}, room=ws_room)

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

                # R2/G6: reconcile denormalized contact stats from truth at
                # call end — the ad-hoc += sites now fire only at row
                # creation; this snaps count/last-interaction/sentiment back
                # to reality no matter what the increments did.
                # R2/R4/R5 via the shared finalizer (F-05): ws-checked
                # contact, stats reconcile, digest, index push — idempotent,
                # so the post-prompt re-running it later is fine.
                from app.services.contact_enrichment import finalize_call_memory
                finalize_call_memory(call)

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
                # To the call's workspace (owner, agents watching AI calls,
                # supervisors) — was a user-room emit + a global broadcast.
                socketio.emit('call_ended', call_ended_data, room=ws_room)
                # And emit call_update with ended status to the workspace
                socketio.emit('call_update', {'call': call_data}, room=ws_room)

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
                    }, room=ws_room)
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


# Transcript text of the AI→human handoff marker row (speaker='system').
# Inserted by _process_utterance_event at the first human-agent utterance that
# follows AI utterances, so transcripts show where the human took over. The
# frontends render 'system' rows as a divider, and
# Transcription.get_full_transcript excludes them (nobody said this out loud).
HANDOFF_MARKER_TEXT = 'Human agent took over the call'


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

    # Map role to speaker format expected by frontend. 'remote-caller' is
    # always the caller; the other side is whoever is handling the call RIGHT
    # NOW. The same live_transcribe session spans the AI→human handoff (it's
    # started once on the caller's A-leg and survives the conference join —
    # see queue_dispatch.enqueue_and_build_swml), so the decision must be made
    # per-utterance from Call.handler_type, never derived once per session.
    # handler_type (not CallLeg) is the authority here: the queue handoff path
    # never closes the ai_agent leg, so "an AI leg is active" stays true long
    # after a human took over.
    if role == 'remote-caller':
        speaker = 'caller'
    elif call.handler_type == 'ai':
        speaker = 'ai'
    else:
        speaker = 'agent'

    # AI→human handoff marker: when the first human-side utterance lands after
    # AI-side ones, insert a 'system' divider row ahead of it so the transcript
    # shows where the human picked up. Detecting the boundary from persisted
    # rows keeps this agnostic to which control path did the handoff (queue
    # take, direct takeover, conference join) and self-limiting — once the
    # marker exists, the previous non-caller row is 'system' or 'agent', so it
    # can't re-fire.
    handoff_marker = None
    if speaker == 'agent':
        prev_non_caller = db.session.query(Transcription).filter(
            Transcription.call_id == call.id,
            Transcription.speaker.in_(('ai', 'agent', 'system')),
        ).order_by(Transcription.sequence_number.desc()).first()
        if prev_non_caller is not None and prev_non_caller.speaker == 'ai':
            handoff_marker = Transcription(
                call_id=call.id,
                transcript=HANDOFF_MARKER_TEXT,
                is_final=True,
                sequence_number=sequence,
                speaker='system',
                language=language
            )
            db.session.add(handoff_marker)
            sequence += 1

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
    # joined this room). The handoff marker goes out first so live viewers see
    # the divider land in the same spot history readers will.
    if handoff_marker is not None:
        socketio.emit('transcription', {
            'call_sid': call_id,
            'text': HANDOFF_MARKER_TEXT,
            'confidence': None,
            'is_final': True,
            'sequence': handoff_marker.sequence_number,
            'role': 'system',
            'speaker': 'system',
            'timestamp': timestamp
        }, room=call_id)
    transcription_data = {
        'call_sid': call_id,
        'text': text,
        'confidence': confidence,
        'is_final': is_final,
        'sequence': sequence,
        'role': role,
        # 'caller' | 'ai' | 'agent' — mapped from role + current handler_type
        'speaker': speaker,
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
        logger.info("Payload shape: %s", request_summary(request, data))
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
        logger.info("Payload shape: %s", request_summary(request, data))
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
        logger.info("Payload shape: %s", request_summary(request, data))
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
                    from app.services.interaction_timeline import best_effort, record_human_started
                    # Native queue callbacks identify the customer leg but do
                    # not reliably include our User id. Preserve that truth as
                    # an unattributed human segment instead of guessing.
                    best_effort(record_human_started, call, call.assigned_agent_id)
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
            else:
                # A-3: the abandoned-in-queue terminal for bridge-transport
                # queues arrives HERE and nowhere else — finalize the caller's
                # memory. Outside the try/except above so a finalize failure
                # can't trigger that rollback (it handles its own).
                from app.services.contact_enrichment import finalize_call_memory
                finalize_call_memory(call)

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
            # Workspace room (§8.1). Unresolvable calls fall back to the
            # default workspace's room — in hosted mode that's platform
            # operators only (nothing leaks to visitors), in clone-and-own
            # it's the whole floor as before.
            from app.services.ws_rooms import workspace_room
            socketio.emit('queue_update', {
                'call': call.to_dict() if call else {'signalwireCallSid': call_id},
                'queue_id': queue_name,
                'action': action,
                'position': position,
                'size': size,
                'wait_time': wait_time,
                'native_queue_event': result,
            }, room=workspace_room(call.workspace_id if call else None))
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
        logger.info("Payload shape: %s", request_summary(request, data))
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
        logger.info("Payload shape: %s", request_summary(request, data))
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

        # Choose the collection from the call's queue (same derivation as the
        # Factbook auto-search). global_data carries call_db_id + queue_slug
        # from coach.py; the Call row is the trusted workspace anchor — this
        # is a public webhook, so nothing else in the payload picks a tenant.
        global_data = data.get('global_data') or {}
        collection_name = DEFAULT_KB_COLLECTION
        call_db_id = global_data.get('call_db_id')
        if isinstance(call_db_id, int) or (isinstance(call_db_id, str) and call_db_id.isdigit()):
            kb_call = db.session.get(Call, int(call_db_id))
            if kb_call is not None:
                queue_slug = global_data.get('queue_slug') or kb_call.queue_id
                collection_name = kb_collection_for_queue(
                    queue_slug, kb_call.workspace_id)

        ai_agents_url = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')
        try:
            resp = http_requests.post(
                f"{ai_agents_url}/search",
                # F-01: the admin API is authenticated now — without these
                # creds this lookup 401s. (Missed on the first pass: this call
                # site is nested deeper than the other /search callers.)
                auth=internal_service_auth(),
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
        logger.info("Payload shape: %s", request_summary(request, data))
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

        # Find the call in database.
        # F-07: FOR UPDATE — concurrent post-prompts (triage + specialist
        # sessions of one call can end near-simultaneously) and the human
        # wrap-up PUT all read-merge-write the same JSON/wrap-up columns;
        # the row lock serializes them. SQLite (tests) ignores it.
        call = (
            db.session.query(Call)
            .filter_by(signalwire_call_sid=call_id)
            .with_for_update()
            .first()
        ) if call_id else None

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
                # G10: latest AI session wins (the specialist's 'resolved'
                # must beat triage's 'transferred') — but a human's saved
                # disposition (wrap_up_source='agent') is never overwritten.
                if raw_disp and (
                    not call.disposition_code or call.wrap_up_source != 'agent'
                ):
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
                if post_mortem:
                    if not call.agent_notes:
                        call.agent_notes = post_mortem
                        ai_filled_wrapup = True
                        logger.info(
                            f"post_prompt: AI post_mortem auto-filled into agent_notes "
                            f"for call {call.id} ({len(post_mortem)} chars)"
                        )
                    elif (
                        call.wrap_up_source != 'agent'
                        and post_mortem not in call.agent_notes
                    ):
                        # G10: a second AI session (specialist after triage)
                        # used to be dropped by first-write-wins. Append it
                        # labeled instead — never touch a human-owned note.
                        call.agent_notes = (
                            f"{call.agent_notes}\n\n[{app_name or 'AI session'}] {post_mortem}"
                        )
                        ai_filled_wrapup = True
                # Explicit provenance for the "Captured by AI" badge — stamped
                # only when we actually auto-filled this pass and a human hasn't
                # already claimed the wrap-up.
                if ai_filled_wrapup and not call.wrap_up_source:
                    call.wrap_up_source = 'ai'

            # G10: a call can get post-prompts from BOTH the receptionist and
            # a specialist. Keep every session's structured assessment keyed
            # by agent (parsed_summary stays last-write for compatibility).
            if parsed_summary and len(parsed_summary) > 0:
                sessions = merged_context.get('session_summaries') or {}
                # F-07: the same agent can produce more than one session on
                # a call (hand-back flows) — uniquify instead of overwrite.
                session_key = app_name or f"session-{len(sessions) + 1}"
                if session_key in sessions:
                    suffix = str(ai_session_id)[:8] if ai_session_id else str(len(sessions) + 1)
                    session_key = f"{session_key}#{suffix}"
                sessions[session_key] = parsed_summary[0]
                merged_context['session_summaries'] = sessions
            call.ai_context = json.dumps(merged_context)

            # Per-call language safety net (maria_language_memory row 29):
            # the enqueue path only runs when a call routes to a HUMAN queue,
            # so AI-only calls ended with caller_language NULL even when the
            # set_caller_language tool had put 'es-ES' into global_data. The
            # tool now write-throughs in real time (/api/calls/<id>/caller-
            # language), but post-prompt still back-fills for the paths where
            # that POST failed or the tool never fired at all — preferring
            # the tool-written global_data value over the post-prompt LLM's
            # assessment, both gated through normalize_language (PGI: model
            # output is unverified input).
            from app.services.call_language import normalize_language
            back_filled_language = None
            if not call.caller_language:
                learned_language = (
                    normalize_language(merged_context.get('caller_language'))
                    or normalize_language(ai_assessment.get('caller_language'))
                )
                if learned_language:
                    call.caller_language = learned_language
                    back_filled_language = learned_language
                    logger.info(
                        f"post_prompt: seeded caller_language={learned_language} "
                        f"for call {call.id}"
                    )

            # Prose summary: first writer keeps the field; later AI sessions
            # append a labeled section instead of being dropped (G10).
            if raw_summary:
                if not call.summary:
                    call.summary = raw_summary
                elif raw_summary.strip() and raw_summary.strip() not in call.summary:
                    call.summary = (
                        f"{call.summary}\n\n[{app_name or 'AI session'}]\n{raw_summary}"
                    )

            # R2 (CONTEXT_AUDIT_2026-08-04): lift learned identity to the
            # Contact so AI-only calls finally update durable memory — same
            # no-clobber rules as the queue-route writer (shared helper).
            # Deliberately never writes Contact.notes.
            if call.contact_id:
                try:
                    from app.models import Contact
                    from app.services.contact_enrichment import (
                        apply_learned_contact_fields,
                    )
                    # F-07: lock the contact row too — enrichment is a
                    # read-check-write (no-clobber) sequence.
                    contact = (
                        db.session.query(Contact)
                        .filter_by(id=call.contact_id)
                        .with_for_update()
                        .first()
                    )
                    # F-02: refuse cross-workspace enrichment through a
                    # mis-bound contact_id.
                    if contact is not None and contact.workspace_id != call.workspace_id:
                        logger.warning(
                            "post_prompt: call %s bound to foreign-workspace "
                            "contact %s — skipping enrichment", call.id, contact.id,
                        )
                        contact = None
                    if contact is not None and apply_learned_contact_fields(
                        contact,
                        {
                            'customer_name': (
                                ai_assessment.get('customer_name')
                                or merged_context.get('customer_name')
                            ),
                            'company': (
                                ai_assessment.get('company')
                                or merged_context.get('company')
                            ),
                            'caller_language': (
                                call.caller_language
                                # Normalized: raw model output ('Spanish',
                                # 'null') must not become the contact's
                                # durable preferred_language.
                                or normalize_language(
                                    merged_context.get('caller_language')
                                )
                            ),
                        },
                    ):
                        logger.info(
                            f"post_prompt: contact {contact.id} enriched from AI summary"
                        )
                    # Digest/index regeneration moved to the handler TAIL
                    # (F-04): at this point the outcome routing below may not
                    # have made the call terminal yet, and the AI-only
                    # self-ended path would otherwise be skipped forever.
                except Exception as e:
                    logger.warning(
                        f"post_prompt: contact enrichment failed (non-fatal): {e}"
                    )

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
                    # PGI: the outcome field is MODEL output — never drive
                    # call state from it. When a human transfer really
                    # happened, /route (concurrent with this post-prompt)
                    # sets status='waiting' + queue_id + conference_name
                    # itself, so writing 'waiting' here was redundant at
                    # best. At worst the model mislabels an AI-specialist
                    # handoff as "transferred_to_human" (observed: call 44,
                    # 2026-08-11 — tool trail shows transfer_to_ai_specialist,
                    # no /route hit) and this branch parked a never-queued
                    # call in a phantom 'waiting', which the next session's
                    # close then classified 'abandoned_in_queue'. Leave
                    # status to hard state; record the claim in the log only.
                    logger.info(
                        f"Call {call_id} AI session ended, model reports "
                        f"human handoff — leaving status to hard state "
                        f"({call.status}); /route owns the 'waiting' flip"
                    )
                elif not call.assigned_agent_id and not call.conference_name:
                    call.update_status('ended')
                    logger.info(
                        f"Call {call_id} AI session ended (outcome={outcome!r}, "
                        f"no agent/conference) — call closed"
                    )

            db.session.commit()
            logger.info(f"✓ Updated call {call.id} with post_prompt data (status: {call.status})")

            # If the language back-fill just learned the language of a call
            # that is STILL live (AI→AI transfer chains: the triage session's
            # post-prompt lands while the specialist is mid-conversation),
            # restart transcription now so the rest of the call transcribes
            # in the right language instead of staying garbled en-US.
            # Post-outcome-routing status guard: a call this handler just
            # closed, or parked 'waiting' for a human, is not restarted.
            if back_filled_language and call.status in ('answered', 'ai_active'):
                try:
                    from app.services.call_language import (
                        restart_ai_leg_transcription,
                    )
                    from app.utils.url_utils import get_base_url
                    restart_ai_leg_transcription(call, base_url=get_base_url())
                except Exception as e:
                    logger.warning(
                        f"post_prompt: transcription language restart failed "
                        f"(non-fatal) for call {call.id}: {e}"
                    )

            # F-04: memory finalization runs AFTER the outcome routing above
            # — an AI-only call this handler itself just closed (the path
            # that never gets a separate call-status webhook) is terminal by
            # now, so its digest/index/stats land. Idempotent with the
            # call-status finalizer in either arrival order.
            from app.services.contact_enrichment import finalize_call_memory
            finalize_call_memory(call)

            # Log the webhook event
            WebhookEvent.log_event(
                event_type="post_prompt_received",
                payload=data,
                call_id=call.id
            )

            # Event Stream `ai_tool_call` backfill. The platform lists every
            # tool the model invoked this session, with parsed arguments, in
            # swaig_log — and post_prompt fires unconditionally, so this is
            # the source that needs no flags on either deployment shape.
            #
            # ORDER IS LOAD-BEARING: this runs BEFORE the emit_call_update
            # below. On a call this handler just closed (AI-only, no separate
            # call-status webhook), that update carries a terminal status —
            # and the desktop drops terminal calls from activeCalls, which
            # unmounts the very panel these events render in. Emitted after,
            # they arrive at a dead component. Each one is also persisted (see
            # _emit_swaig_tool_call) so Call Detail can show them once the
            # live panel is gone either way.
            swaig_log = data.get('swaig_log')
            if isinstance(swaig_log, list):
                emitted = sum(
                    1 for entry in swaig_log
                    if _emit_swaig_tool_call(entry, source='post_prompt',
                                             call=call)
                )
                if emitted:
                    logger.info(
                        f"post_prompt: emitted {emitted} ai_tool_call "
                        f"event(s) for call {call.id}"
                    )

            # Emit call_update so frontend sees the status change
            from app.services.callcenter_socketio import emit_call_update
            emit_call_update(call)

            # Only emit call_ended if the call is actually ended/completed
            # If status is 'waiting', the call is still active and should stay in the queue
            if call.status in ('ended', 'completed'):
                from app.services.ws_rooms import workspace_room
                call_ended_data = {
                    'callId': call.id,
                    'call_sid': call_id,
                    'reset_ui': True
                }
                socketio.emit('call_ended', call_ended_data,
                              room=workspace_room(call.workspace_id))

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
    captures/debug-events.jsonl and skip DB work on all of them except the
    handful of ``swaig_call`` (tool invocation) events, which additionally
    emit an ``ai_tool_call`` to the live Event Stream. Inspect the file after a
    test call to see exactly what the agent did, turn by turn — e.g. a burst of
    step_change/filler events is the "skips to the final step + spams fillers"
    symptom, and the llm_request/llm_response pair shows why the model jumped.
    """
    try:
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict() if request.form else {}
        # The event NAME is the payload's own non-`call_info` key
        # (`swaig_call`, `llm_response`, `conversation_add`, ...). There is no
        # 'label'/'action' field — both read None on every captured payload,
        # so this log line was a hardcoded "unknown" before.
        call_info = data.get('call_info') or {}
        event_type = next((k for k in data if k != 'call_info'), 'unknown')
        call_id = data.get('call_id') or call_info.get('call_id')
        # Stream only — the "latest single event" isn't useful; the sequence is.
        capture_webhook_payload('debug-events', data, latest=False)
        logger.info(f"DEBUG EVENT [{event_type}] call={call_id}")

        # A `swaig_call` IS a tool invocation — surface it on the live Event
        # Stream. Only ~6 of ~290 events per call are swaig_calls, so the one
        # sid lookup each costs does not reintroduce the per-event DB work
        # this handler deliberately avoids.
        if isinstance(data.get('swaig_call'), dict):
            _emit_swaig_tool_call(data['swaig_call'], source='debug_events')
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
        logger.info("Payload shape: %s", request_summary(request, data))
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
        logger.info("Payload shape: %s", request_summary(request, data))
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


@webhooks_bp.route('/sms-inbound', methods=['POST'])
@require_webhook_auth
def sms_inbound():
    """Inbound SMS webhook — demo phone verification via texted pairing code.

    Point the demo number's "message received" webhook at this route (sign it
    with the WEBHOOK_AUTH creds like every other webhook URL). The visitor's
    dashboard shows a 6-digit code and tells them to TEXT it to the demo
    number; when the MO message lands here we match the code and bind the
    sender's number to their demo persona (services/demo_verify).

    This direction needs NO messaging campaign — 10DLC/A2P registration gates
    outbound application messages, and we never reply. The sender number on an
    inbound SMS is also a stronger possession proof than voice caller-ID.

    Accepts both webhook shapes: Compatibility form fields (From/To/Body) and
    JSON (from_number/body). Always answers 200 with an empty LaML <Response/>
    so the platform never retries or errors — outcomes are visible to the
    visitor via the demo_phone_verified socket push and the verify/status
    endpoint, not to the texter.
    """
    _laml_ok = ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                200, {'Content-Type': 'application/xml'})

    try:
        from app.utils.demo_config import is_demo_mode
        if not is_demo_mode():
            # Production-shape deployment: acknowledge and drop.
            return _laml_ok

        data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        from_number = (
            data.get('From') or data.get('from_number') or data.get('from') or ''
        )
        body = (data.get('Body') or data.get('body') or '').strip()

        logger.info(
            "WEBHOOK: /api/webhooks/sms-inbound from=%s body=%r",
            from_number, body[:40],
        )

        if not from_number or not body:
            return _laml_ok

        # Extract the first 6-digit group from the message — tolerate
        # "123456", "code 123456", "123 456" etc. (6 digits since
        # DEMO-SEC-06 moved codes to secrets-grade randomness).
        import re as _re
        compact = _re.sub(r'\s', '', body)
        match = _re.search(r'(?<!\d)(\d{6})(?!\d)', compact)
        if not match:
            logger.info("sms-inbound: no 6-digit code in message — ignoring")
            return _laml_ok

        # Per-sender pairing-attempt cap. Code entropy is the real auth on
        # this webhook (the shared To-number carries no session context),
        # so bound guessing explicitly instead of leaning on SMS costing
        # the attacker money. Fail-open on Redis trouble, like the other
        # demo limiters.
        try:
            from app.services.redis_service import get_redis_client
            _r = get_redis_client()
            if _r is not None:
                _cap_key = f'demo:verify:sms_attempts:{from_number}'
                _n = _r.incr(_cap_key)
                if _n == 1:
                    _r.expire(_cap_key, 3600)
                if _n > 10:
                    logger.warning(
                        "sms-inbound: pairing-attempt cap hit for %s — ignoring",
                        from_number,
                    )
                    return _laml_ok
        except Exception:
            pass

        from app.services.demo_verify import pair_number, PAIR_OK
        result = pair_number(match.group(1), from_number)
        logger.info("sms-inbound: pair → %s", result.get('status'))

        # On success, flip the visitor's UI live via their workspace room
        # (§6.2 — the binding is workspace-keyed now, and every member's
        # socket sits in the room).
        if result.get('status') == PAIR_OK and result.get('workspace_id'):
            try:
                from app.services.ws_rooms import workspace_room
                socketio.emit(
                    'demo_phone_verified',
                    {'masked_number': result.get('masked')},
                    room=workspace_room(result['workspace_id']),
                )
            except Exception as exc:
                logger.warning("sms-inbound: socket notify failed: %s", exc)

        return _laml_ok

    except Exception as e:
        logger.error(f"Error processing inbound SMS webhook: {str(e)}")
        # Still 200 — never make the platform retry a verification text.
        return _laml_ok


