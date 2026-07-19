"""
Tap Audio Relay Service

Receives real-time audio from SignalWire's calling.tap via WebSocket,
and relays it to monitoring clients via Socket.IO events.

Flow:
1. Backend calls sw_api.tap_call(call_sid, ws_uri) where ws_uri points here
2. SignalWire connects to /ws/tap-stream/<call_id> and streams raw audio (PCMU/PCMA)
3. This service receives audio frames and emits them as Socket.IO 'tap_audio' events
4. Frontend AudioMonitor component receives and plays via Web Audio API

Audio routing (RT-01, 2026-06-02 audit follow-up):
- All four emits below are SCOPED to ``room=f'tap:{call_id}'``. Without the
  room, Socket.IO broadcasts to every connected client — including
  unauthenticated ones — so live PCMU audio of every active call would leak
  to anyone who happened to have the dashboard open.
- The companion server-side ``join_tap`` Socket.IO handler in
  ``callcenter_socketio.py`` checks the requester's permission (mirroring
  ``call_control.py:start_monitor``) before adding them to the room.
- Frontend AudioMonitor must emit ``join_tap`` (token + call_id) AFTER
  authenticating its socket, BEFORE the audio starts flowing.
"""

from flask_sock import Sock
from app import socketio
from app.utils.url_utils import verify_tap_stream_signature
from flask import request
import logging
import base64
import json

logger = logging.getLogger(__name__)

sock = Sock()


def init_tap_relay(app):
    """Initialize the tap relay WebSocket routes on the Flask app."""
    sock.init_app(app)


def _tap_room(call_id: str) -> str:
    """Canonical room name for the per-call tap audio stream.

    Kept here (not in callcenter_socketio) so the producer (this file) and
    the consumer-authorizer (join_tap handler) can't drift on naming.
    """
    return f'tap:{call_id}'


@sock.route('/ws/tap-stream/<call_id>')
def tap_stream(ws, call_id):
    return _handle_tap_stream(ws, call_id)


def _handle_tap_stream(ws, call_id):
    """WebSocket endpoint that receives tap audio from SignalWire.

    SignalWire sends raw audio frames (PCMU 8kHz by default) over this connection.
    We base64-encode each frame and relay it via Socket.IO to authorized
    monitoring clients (those who passed permission check via ``join_tap``).
    """
    if not verify_tap_stream_signature(
        call_id,
        request.args.get('expires'),
        request.args.get('signature'),
    ):
        logger.warning('Rejected unauthorized tap stream for call %s', call_id)
        # Flask-Sock has already completed the WebSocket handshake before the
        # handler runs. Close immediately, before emitting status or reading a
        # single caller-controlled frame.
        ws.close()
        return

    logger.info(f"Tap stream connected for call {call_id}")
    room = _tap_room(call_id)

    # Emit an event so the frontend knows tap is active. Scoped to the tap
    # room so only authorized monitor clients see the status flip; otherwise
    # an unauthenticated dashboard could enumerate which calls have a tap
    # attached, which is its own information leak.
    socketio.emit('tap_status', {
        'call_id': call_id,
        'status': 'connected',
    }, room=room)

    frame_count = 0
    try:
        while True:
            # Receive raw audio data from SignalWire
            data = ws.receive()

            if data is None:
                logger.info(f"Tap stream closed for call {call_id}")
                break

            frame_count += 1

            # SignalWire sends binary audio frames
            if isinstance(data, bytes):
                # Base64 encode for transmission over Socket.IO
                audio_b64 = base64.b64encode(data).decode('ascii')
                socketio.emit('tap_audio', {
                    'call_id': call_id,
                    'audio': audio_b64,
                    'codec': 'PCMU',
                    'sample_rate': 8000,
                    'frame': frame_count,
                }, room=room)
            elif isinstance(data, str):
                # SignalWire may send JSON metadata frames
                try:
                    meta = json.loads(data)
                    logger.debug(f"Tap metadata for call {call_id}: {meta}")
                    socketio.emit('tap_metadata', {
                        'call_id': call_id,
                        'metadata': meta,
                    }, room=room)
                except json.JSONDecodeError:
                    logger.warning(f"Unexpected text frame on tap {call_id}: {data[:100]}")

    except Exception as e:
        logger.error(f"Tap stream error for call {call_id}: {e}")
    finally:
        logger.info(f"Tap stream ended for call {call_id} ({frame_count} frames received)")
        socketio.emit('tap_status', {
            'call_id': call_id,
            'status': 'disconnected',
        }, room=room)
