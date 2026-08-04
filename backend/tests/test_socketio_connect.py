"""Socket.IO CONNECT must actually run its handler, not just complete a handshake.

REGRESSION (2026-08-04). Flask 3.1 made ``RequestContext.session`` a read-only
property. Flask-SocketIO 5.5.1 assigns ``ctx.session`` in ``_handle_event``, so
with Flask 3.1.3 every CONNECT raised::

    AttributeError: property 'session' of 'RequestContext' object has no setter

Nothing surfaced it. The exception is swallowed by python-socketio's
``run_handler`` as a logged "message handler error", the HTTP layer stayed 200,
and the engine.io handshake still returned a sid — so a transport-level probe
(``GET /socket.io/?EIO=4&transport=polling``) passed while every room join
silently failed and ALL realtime was dead app-wide: no ``call_update``, no
``queue_update``, no ``demo_phone_verified``.

The lesson these tests encode: a handshake proves the transport, NOT that the
connect handler ran. Assert on handler side effects instead.
"""
import flask
import pytest
from flask import Flask
from flask_socketio import SocketIO, emit


@pytest.fixture()
def sio_app():
    app = Flask(__name__)
    app.config.update(SECRET_KEY='test-secret', TESTING=True)
    sio = SocketIO(app, async_mode='threading')
    calls = {'connects': 0, 'errors': []}

    @sio.on('connect')
    def _on_connect(auth=None):
        # manage_session defaults True, which is the path that touches
        # ctx.session / ctx._session — the exact line that used to raise.
        try:
            flask.session['touched'] = True
            calls['connects'] += 1
        except Exception as exc:  # noqa: BLE001
            calls['errors'].append(repr(exc))
            raise

    @sio.on('ping_me')
    def _on_ping(data=None):
        emit('pong_back', {'ok': True})

    return app, sio, calls


def test_connect_handler_actually_runs(sio_app):
    """The regression: CONNECT completed at transport level while the handler
    blew up. Assert the handler body ran to completion."""
    app, sio, calls = sio_app
    client = sio.test_client(app)

    assert client.is_connected(), 'socket did not connect'
    assert not calls['errors'], f"connect handler raised: {calls['errors']}"
    assert calls['connects'] == 1, (
        'connect handler did not complete — this is the Flask 3.1 / '
        'Flask-SocketIO ctx.session incompatibility'
    )
    client.disconnect()


def test_session_write_in_connect_does_not_raise(sio_app):
    """Narrowest form of the bug: writing flask.session inside a CONNECT
    handler is what assigned ctx.session and hit the missing setter."""
    app, sio, calls = sio_app
    client = sio.test_client(app)
    assert calls['errors'] == []
    client.disconnect()


def test_events_round_trip_after_connect(sio_app):
    """A failed CONNECT also breaks everything downstream, so prove an event
    survives the round trip rather than only checking connection state."""
    app, sio, _ = sio_app
    client = sio.test_client(app)
    client.emit('ping_me', {})
    received = client.get_received()
    assert any(r['name'] == 'pong_back' for r in received), (
        f'no pong_back in {[r["name"] for r in received]}'
    )
    client.disconnect()


def test_flask_socketio_does_not_assign_readonly_session_attr():
    """Guard the dependency pin itself.

    Flask >= 3.1.3 exposes ``RequestContext._session``; a Flask-SocketIO that
    only knows how to assign the public ``session`` attribute cannot work with
    it. Fails loudly if requirements.txt ever moves back to such a version.
    """
    import inspect

    import flask_socketio

    src = inspect.getsource(flask_socketio.SocketIO._handle_event)
    if 'ctx.session = ' in src:
        # Must be `ctx._session`, checked exactly — a loose `'_session' in src`
        # matches the substring inside `manage_session` and passes on the
        # broken version, which is how this guard first fooled itself.
        assert 'ctx._session' in src, (
            'Flask-SocketIO assigns ctx.session with no ctx._session branch — '
            'incompatible with Flask >= 3.1.3. Pin Flask-SocketIO >= 5.6.1.'
        )
