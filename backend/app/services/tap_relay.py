"""
Tap Audio Relay Service

Receives real-time audio from SignalWire's calling.tap via WebSocket,
and relays it to monitoring clients via Socket.IO events.

Flow:
1. Backend calls sw_api.tap_call(call_sid, ws_uri) where ws_uri points here
2. SignalWire connects to /ws/tap-stream/<call_id> and streams raw audio (PCMU/PCMA)
3. This service receives audio frames and emits them as Socket.IO 'tap_audio' events
4. Frontend AudioMonitor component receives and plays via Web Audio API
"""

from flask_sock import Sock
from app import socketio
import logging
import base64
import json

logger = logging.getLogger(__name__)

sock = Sock()


def init_tap_relay(app):
    """Initialize the tap relay WebSocket routes on the Flask app."""
    sock.init_app(app)


@sock.route('/ws/tap-stream/<call_id>')
def tap_stream(ws, call_id):
    """WebSocket endpoint that receives tap audio from SignalWire.

    SignalWire sends raw audio frames (PCMU 8kHz by default) over this connection.
    We base64-encode each frame and relay it via Socket.IO to any monitoring clients.

    The call_id is used to target the Socket.IO room so only the
    monitoring client for this specific call receives the audio.
    """
    logger.info(f"Tap stream connected for call {call_id}")

    # Emit an event so the frontend knows tap is active
    socketio.emit('tap_status', {
        'call_id': call_id,
        'status': 'connected',
    })

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
                })
            elif isinstance(data, str):
                # SignalWire may send JSON metadata frames
                try:
                    meta = json.loads(data)
                    logger.debug(f"Tap metadata for call {call_id}: {meta}")
                    socketio.emit('tap_metadata', {
                        'call_id': call_id,
                        'metadata': meta,
                    })
                except json.JSONDecodeError:
                    logger.warning(f"Unexpected text frame on tap {call_id}: {data[:100]}")

    except Exception as e:
        logger.error(f"Tap stream error for call {call_id}: {e}")
    finally:
        logger.info(f"Tap stream ended for call {call_id} ({frame_count} frames received)")
        socketio.emit('tap_status', {
            'call_id': call_id,
            'status': 'disconnected',
        })
