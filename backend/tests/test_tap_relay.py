from flask import Flask

from app.services import tap_relay


class FakeWebSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def receive(self):
        raise AssertionError('unauthorized tap must close before reading frames')


def test_unauthorized_tap_closes_before_emitting_or_reading(monkeypatch):
    app = Flask(__name__)
    websocket = FakeWebSocket()
    emissions = []
    monkeypatch.setattr(tap_relay, 'verify_tap_stream_signature', lambda *_args: False)
    monkeypatch.setattr(tap_relay.socketio, 'emit', lambda *args, **kwargs: emissions.append((args, kwargs)))

    with app.test_request_context('/ws/tap-stream/call-a'):
        tap_relay._handle_tap_stream(websocket, 'call-a')

    assert websocket.closed is True
    assert emissions == []
