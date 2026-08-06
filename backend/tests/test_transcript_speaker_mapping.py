"""Transcript speaker mapping across the AI→human handoff.

One live_transcribe session spans the whole call (started on the caller's
A-leg, surviving the conference join), so ``_process_utterance_event`` must
decide the non-caller speaker PER UTTERANCE from ``Call.handler_type``:
'ai' while the AI is handling, 'agent' once a human is. It also inserts a
single synthetic ``speaker='system'`` marker row at the boundary so
transcripts show where the human took over.
"""

import pytest
from flask import Flask

from app import db
from app.api import webhooks as webhooks_module
from app.api.webhooks import HANDOFF_MARKER_TEXT, _process_utterance_event
from app.models import Call, Transcription, User, Workspace


class _RecordingSocketIO:
    """Stands in for app.socketio; records emitted events in order."""

    def __init__(self):
        self.events = []

    def emit(self, event, data=None, room=None, **kwargs):
        self.events.append({'event': event, 'data': data, 'room': room})


@pytest.fixture()
def webhook_app(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    fake_socketio = _RecordingSocketIO()
    monkeypatch.setattr(webhooks_module, 'socketio', fake_socketio)
    with app.app_context():
        db.create_all()
        yield app, fake_socketio
        db.session.remove()
        db.drop_all()


def _make_call(handler_type='ai', sid='call-speaker-test'):
    workspace = Workspace(name='Speaker mapping test')
    db.session.add(workspace)
    db.session.flush()
    agent = User(
        workspace_id=workspace.id,
        email='agent@example.test',
        password_hash='not-used',
        name='Agent One',
    )
    db.session.add(agent)
    db.session.flush()
    call = Call(
        workspace_id=workspace.id,
        user_id=agent.id,
        signalwire_call_sid=sid,
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type=handler_type,
        ai_agent_name='Support AI' if handler_type == 'ai' else None,
        status='ai_active' if handler_type == 'ai' else 'active',
    )
    db.session.add(call)
    db.session.commit()
    return call


def _utterance_payload(sid, text, role):
    return {
        'call_info': {'call_id': sid},
        'utterance': {
            'content': text,
            'confidence': 0.97,
            'role': role,
            'lang': 'en-US',
            'timestamp': 1722790000,
            'final': True,
        },
        'channel_data': {},
    }


def _process(sid, text, role):
    handled, status = _process_utterance_event(
        _utterance_payload(sid, text, role), source='transcription'
    )
    assert handled and status == 'ok'


def _rows(call):
    return (
        db.session.query(Transcription)
        .filter_by(call_id=call.id)
        .order_by(Transcription.sequence_number.asc())
        .all()
    )


def test_ai_handled_call_maps_non_caller_to_ai(webhook_app):
    call = _make_call(handler_type='ai')

    _process(call.signalwire_call_sid, 'Hi, I would like to upgrade.', 'remote-caller')
    _process(call.signalwire_call_sid, 'Happy to help with that upgrade!', 'local-caller')

    speakers = [(t.speaker, t.transcript) for t in _rows(call)]
    assert speakers == [
        ('caller', 'Hi, I would like to upgrade.'),
        ('ai', 'Happy to help with that upgrade!'),
    ]


def test_human_handled_call_maps_non_caller_to_agent(webhook_app):
    call = _make_call(handler_type='human')

    _process(call.signalwire_call_sid, 'Hello?', 'remote-caller')
    _process(call.signalwire_call_sid, 'Hi, this is Dana from support.', 'local-caller')

    speakers = [t.speaker for t in _rows(call)]
    assert speakers == ['caller', 'agent']


def test_null_handler_type_falls_back_to_agent(webhook_app):
    """Returned-to-queue calls set handler_type=None; keep today's 'agent'."""
    call = _make_call(handler_type='human')
    call.handler_type = None
    db.session.commit()

    _process(call.signalwire_call_sid, 'Please hold.', 'local-caller')

    assert _rows(call)[-1].speaker == 'agent'


def test_handoff_inserts_single_system_marker_at_the_boundary(webhook_app):
    call = _make_call(handler_type='ai')
    sid = call.signalwire_call_sid

    _process(sid, 'I need a human please.', 'remote-caller')
    _process(sid, 'Connecting you to a specialist.', 'local-caller')

    # The handoff: takeover/queue-take flips the call's handler to human.
    call.handler_type = 'human'
    db.session.commit()

    _process(sid, 'Hi, I can take it from here.', 'local-caller')
    _process(sid, 'Thanks!', 'remote-caller')
    _process(sid, 'What can I do for you?', 'local-caller')

    rows = _rows(call)
    assert [(t.speaker, t.transcript) for t in rows] == [
        ('caller', 'I need a human please.'),
        ('ai', 'Connecting you to a specialist.'),
        ('system', HANDOFF_MARKER_TEXT),
        ('agent', 'Hi, I can take it from here.'),
        ('caller', 'Thanks!'),
        ('agent', 'What can I do for you?'),
    ]
    # Marker is final and sequence numbers stay strictly increasing.
    marker = rows[2]
    assert marker.is_final is True
    assert [t.sequence_number for t in rows] == list(range(6))


def test_no_marker_when_call_never_had_an_ai_phase(webhook_app):
    call = _make_call(handler_type='human')

    _process(call.signalwire_call_sid, 'Hello.', 'remote-caller')
    _process(call.signalwire_call_sid, 'Support, how can I help?', 'local-caller')
    _process(call.signalwire_call_sid, 'Sure, one moment.', 'local-caller')

    assert all(t.speaker != 'system' for t in _rows(call))


def test_socket_emits_marker_then_agent_utterance(webhook_app):
    _, fake_socketio = webhook_app
    call = _make_call(handler_type='ai')
    sid = call.signalwire_call_sid

    _process(sid, 'Let me transfer you.', 'local-caller')
    call.handler_type = 'human'
    db.session.commit()
    _process(sid, 'Agent here.', 'local-caller')

    transcription_events = [
        e for e in fake_socketio.events if e['event'] == 'transcription'
    ]
    assert [e['data']['speaker'] for e in transcription_events] == [
        'ai', 'system', 'agent',
    ]
    marker_event = transcription_events[1]
    assert marker_event['data']['text'] == HANDOFF_MARKER_TEXT
    assert marker_event['data']['is_final'] is True
    assert marker_event['room'] == sid
    # Marker sequence slots directly before the utterance that triggered it.
    assert marker_event['data']['sequence'] + 1 == transcription_events[2]['data']['sequence']


def test_get_full_transcript_excludes_marker_rows(webhook_app):
    call = _make_call(handler_type='ai')
    sid = call.signalwire_call_sid

    _process(sid, 'I want to cancel.', 'remote-caller')
    _process(sid, 'Let me get a human.', 'local-caller')
    call.handler_type = 'human'
    db.session.commit()
    _process(sid, 'I can help with the cancellation.', 'local-caller')

    full = Transcription.get_full_transcript(call.id)
    assert HANDOFF_MARKER_TEXT not in full
    assert full == (
        'I want to cancel. Let me get a human. I can help with the cancellation.'
    )
