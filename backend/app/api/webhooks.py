from flask import request, jsonify
from app import db, socketio
from app.api import webhooks_bp
from app.models import Call, Transcription, WebhookEvent
from app.services.redis_service import publish_event
import logging
import json

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


@webhooks_bp.route('/call-status', methods=['POST'])
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
            call.update_status(mapped_status)

            # Update from_number if provided and not already set
            if from_number and not call.from_number:
                call.from_number = from_number
                logger.info(f"Updated call {call_id} with from_number: {from_number}")

            db.session.commit()

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
                'startTime': call.created_at.isoformat() if call.created_at else None,
                'created_at': call.created_at.isoformat() if call.created_at else None,
                'answered_at': call.answered_at.isoformat() if call.answered_at else None,
                'ended_at': call.ended_at.isoformat() if call.ended_at else None,
                'user_id': call.user_id,
                'queueId': 'general'  # TODO: Determine from call routing
            }

            # Emit to call-specific room
            socketio.emit('call_status', call_data, room=call_id)

            # Also emit to user room for CallsList updates
            socketio.emit('call_status', call_data, room=str(call.user_id))

            # Emit call_update for Agent Dashboard
            socketio.emit('call_update', {'call': call_data}, room=str(call.user_id))

            # Special handling for ended status to reset UI
            if mapped_status == 'ended':
                socketio.emit('call_ended', {
                    'callId': call.id,  # Use database ID
                    'call_sid': call_id,  # Also provide SignalWire ID
                    'reset_ui': True
                }, room=str(call.user_id))

        return '', 200

    except Exception as e:
        logger.error(f"Error processing call status webhook: {str(e)}")
        return '', 500


@webhooks_bp.route('/transcription', methods=['POST'])
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
                    'timestamp': timestamp
                }

                # Emit to call room (all agents viewing this call have joined this room)
                socketio.emit('transcription', transcription_data, room=call_id)
                logger.info(f"✓ Emitted transcription to call room {call_id}")
            else:
                logger.warning(f"Call not found for ID: {call_id}")

        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        logger.error(f"Error processing transcription webhook: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/summary', methods=['POST'])
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


@webhooks_bp.route('/recording', methods=['POST'])
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


