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

logger = logging.getLogger(__name__)


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

                # Close any active or connecting call legs
                active_leg = CallLeg.get_active_leg(call.id)
                if not active_leg:
                    # Also check for legs stuck in 'connecting' (e.g., takeover legs
                    # that were created but the agent hadn't fully connected yet)
                    active_leg = db.session.query(CallLeg).filter_by(
                        call_id=call.id,
                        status='connecting'
                    ).first()
                if active_leg:
                    active_leg.end_leg(reason='hangup')
                    db.session.commit()
                    logger.info(f"Closed leg {active_leg.id} (was {active_leg.status}) for call {call.id}")

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


@webhooks_bp.route('/transcription', methods=['POST'])
@require_webhook_auth
def transcription():
    """Handle live transcription webhook from SignalWire."""
    try:
        data = request.get_json() if request.is_json else request.form.to_dict()

        # Log the complete JSON received
        logger.info("="*50)
        logger.info("WEBHOOK: /api/webhooks/transcription")
        logger.info(f"RAW JSON: {json.dumps(data, indent=2)}")
        logger.info("="*50)

        # Extract call_id from the nested structure
        call_info = data.get('call_info', {})
        call_id = call_info.get('call_id')

        # Check if this is an utterance (transcript) event
        utterance = data.get('utterance', {})

        if not call_id:
            # Try channel_data as fallback
            channel_data = data.get('channel_data', {})
            call_id = channel_data.get('call_id')

        logger.info(f"Extracted - Call ID: {call_id}, Has utterance: {bool(utterance)}")

        # Log the webhook event (use call.id if we can find it)
        call = Call.find_by_sid(call_id) if call_id else None
        WebhookEvent.log_event(
            event_type="transcription",
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

        if utterance and call_id:
            # Extract transcript data from utterance
            text = utterance.get('content', '')
            confidence = utterance.get('confidence', 0)
            role = utterance.get('role', 'unknown')
            language = utterance.get('lang', 'en-US')
            timestamp = utterance.get('timestamp', 0)

            # Check if transcription is final (not partial)
            # With partial_events: False in SWML, we should only get final transcriptions
            # But check utterance for 'final' or 'is_final' field just in case
            is_final = utterance.get('final', utterance.get('is_final', True))

            # Skip partial transcriptions to avoid duplicates
            if not is_final:
                logger.debug(f"Skipping partial transcription: '{text}'")
                return jsonify({'status': 'skipped', 'reason': 'partial'}), 200

            # Find the call
            if call:
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

                logger.info(f"Saved transcript: '{text}' (confidence: {confidence}, role: {role}, speaker: {speaker})")

                # Emit transcription to both call-specific room and user room
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

                # Emit to call room (all agents viewing this call have joined this room)
                socketio.emit('transcription', transcription_data, room=call_id)
                logger.info(f"✓ Emitted transcription to call room {call_id}")

                # Agent Assist Factbook auto-mode (see AGENT_ASSIST.md).
                # Best-effort; failures must never break transcription persistence.
                if speaker == 'caller' and call.assigned_agent_id:
                    try:
                        _trigger_kb_auto_search_if_enabled(call, call_id)
                    except Exception as e:
                        logger.warning(f"[Factbook auto] trigger failed: {e}")
            else:
                logger.warning(f"Call not found for ID: {call_id}")

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
                logger.warning(f"✗ Call not found in database for ID: {call_id}")
                logger.info("Checking all calls in DB for debugging...")
                from app.models.call import Call
                all_calls = db.session.query(Call).order_by(Call.created_at.desc()).limit(10).all()
                for c in all_calls:
                    logger.info(f"  - Call ID {c.id}: SID={c.signalwire_call_sid}, Status={c.status}, Created={c.created_at}")

                # Try to create the call if it doesn't exist (for direct webhook calls)
                logger.info(f"Attempting to create call record for orphaned summary...")
                try:
                    from app.models import User
                    system_user = User.find_by_email('system@signalwire.local')
                    if not system_user:
                        system_user = db.session.query(User).first()

                    if system_user:
                        new_call = Call(
                            signalwire_call_sid=call_id,  # Store the call_id
                            user_id=system_user.id,
                            destination='unknown',
                            destination_type='phone',
                            status='ended',
                            summary=summary_text
                        )
                        db.session.add(new_call)
                        db.session.commit()
                        logger.info(f"✓ Created call record for {call_id} with summary")

                        # Emit the summary now
                        socketio.emit('summary', {
                            'call_sid': call_id,  # Frontend expects call_sid
                            'summary': summary_text
                        }, room=call_id)
                        socketio.emit('summary', {
                            'call_sid': call_id,  # Frontend expects call_sid
                            'summary': summary_text
                        }, room=str(system_user.id))
                except Exception as e:
                    logger.error(f"Failed to create call record: {str(e)}")
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
                    # 'created' fires still closes the row.
                    if call.status in ('pending', 'waiting', 'queued', 'assigned'):
                        call.status = 'ended'
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

        # global_data is set by coach.build_sidecar_start_params and rides on
        # every event (per dev Q4). All downstream routing keys live in here.
        global_data = data.get('global_data') or {}
        call_sid = (
            global_data.get('call_sid')
            or data.get('call_id')
            or (data.get('call_info') or {}).get('call_id')
        )
        agent_user_id = global_data.get('agent_user_id')
        queue_slug = global_data.get('queue_slug')
        coach_mode = global_data.get('coach_mode')
        coach_intensity = global_data.get('coach_intensity')

        # Persist for audit/replay regardless of whether we can route it —
        # missing global_data shouldn't lose the event.
        call = Call.find_by_sid(call_sid) if call_sid else None
        WebhookEvent.log_event(
            event_type="sidecar_event",
            payload=data,
            call_id=call.id if call else None,
        )

        # Classify the event. Sidecar API isn't standardized yet; check the
        # likely field names in order. Fall back to ``kind`` for forward-compat.
        kind = (
            data.get('event')
            or data.get('event_type')
            or data.get('type')
            or data.get('kind')
            or 'unknown'
        )

        # Suggestion-style payloads put the human-readable text in one of
        # these common slots. We forward whichever is present so the UI can
        # render without caring which milestone of the sidecar API this is.
        suggestion_text = (
            data.get('suggestion')
            or data.get('text')
            or data.get('message')
            or (data.get('content') if isinstance(data.get('content'), str) else None)
            or ''
        )

        # ask correlation token (M10) — may not exist until SignalWire ships
        # the ask_id field. Until then we fall back to a FIFO pop against
        # the per-call pending_asks list that coach_ask pushes to.
        ask_id = data.get('ask_id') or data.get('correlation_id')
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
            'timestamp': data.get('timestamp') or data.get('ts'),
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

            # Add parsed summary if available
            if parsed_summary and len(parsed_summary) > 0:
                merged_context['parsed_summary'] = parsed_summary[0]

            call.ai_context = json.dumps(merged_context)

            # If we have a raw summary and no existing summary, use it
            if raw_summary and not call.summary:
                call.summary = raw_summary

            # Clean up any queue entries for this call
            from app.services.queue_service import QueueService
            from app.services.redis_service import get_redis_client
            redis_client = get_redis_client()
            if redis_client:
                queue_svc = QueueService(redis_client)
                queue_svc.remove_call_from_all_queues(call_id)

            # post_prompt fires when the AI conversation ends, NOT when the phone call ends.
            # The caller may still be on the line (e.g., waiting in queue for a human agent).
            # Do NOT mark as 'completed' here — let the call-status webhook handle final status.
            # Instead, transition active calls back to 'waiting' so agents can take them.
            if call.status in ('answered', 'ai_active'):
                call.status = 'waiting'
                logger.info(f"Call {call_id} AI session ended — set to 'waiting' for human pickup")

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

        # Log the webhook event
        WebhookEvent.log_event(
            event_type="recording_completed",
            payload=data,
            call_id=call_id  # This should be the database ID ideally
        )

        # Emit recording URL via WebSocket
        if recording_url:
            socketio.emit('recording', {
                'call_sid': call_id,  # Frontend expects call_sid
                'recording_url': recording_url,
                'recording_sid': recording_sid
            }, room=call_id)

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


