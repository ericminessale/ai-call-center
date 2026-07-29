"""Hold timeout on the in-conference announcement loop.

The loop used to be an unbounded ``while True`` whose only exit was the caller
dropping out of the queue zset, and ``Queue.max_wait_before_ai_fallback`` was
stored, editable, and read by nothing. These tests pin the contract that it is
now enforced: past the cap the caller is enrolled in the callback queue, told
so, and released — and the loop stops.
"""

from datetime import datetime, timedelta
import json

import pytest
from flask import Flask

from app import db
from app.models import Call, Callback, Queue, User, Workspace
from app.services import call_watchdog, queue_dispatch
from app.services import redis_service, signalwire_api


CALL_SID = 'call-hold-timeout-test'
QUEUE_SLUG = 'support'


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

@pytest.fixture()
def hold_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


class FakeQueueZset:
    """Stands in for Redis. ``zrange`` serves one scripted reply per pass so a
    test can make the caller leave the queue on a chosen iteration."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def zrange(self, _key, _start, _stop):
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return []


class FakeSignalWire:
    def __init__(self, tts_error=None):
        self.tts = []
        self.ended = []
        self.tts_error = tts_error

    def play_tts(self, call_sid, text, **_kwargs):
        self.tts.append((call_sid, text))
        if self.tts_error is not None:
            raise self.tts_error
        return {'ok': True}

    def end_call(self, call_sid):
        self.ended.append(call_sid)
        return {'ok': True}


def _entry(*, waited_seconds, call_sid=CALL_SID):
    """A queue-zset member for a caller who has been waiting that long."""
    enqueued_at = datetime.utcnow() - timedelta(seconds=waited_seconds)
    return json.dumps({
        'call_id': call_sid,
        'queue_id': QUEUE_SLUG,
        'priority': 5,
        'context': {'customer_name': 'Dana Reyes', 'reason': 'billing dispute'},
        'caller_info': {'number': '+15555550123', 'name': 'Dana Reyes'},
        'enqueued_at': enqueued_at.isoformat(),
    })


def _seed(*, max_wait=120, from_number='+15555550123'):
    """Workspace + queue + a waiting inbound call. Returns (workspace, call)."""
    workspace = Workspace(name='Hold timeout test')
    db.session.add(workspace)
    db.session.flush()

    owner = User(
        workspace_id=workspace.id,
        email='agent@example.test',
        password_hash='not-used',
        name='Agent One',
    )
    db.session.add(owner)
    db.session.flush()

    db.session.add(Queue(
        workspace_id=workspace.id,
        slug=QUEUE_SLUG,
        display_name='Support',
        routing_strategy='round_robin',
        max_wait_before_ai_fallback=max_wait,
    ))
    call = Call(
        workspace_id=workspace.id,
        user_id=owner.id,
        signalwire_call_sid=CALL_SID,
        from_number=from_number,
        destination='+15555559999',
        destination_type='phone',
        direction='inbound',
        handler_type='human',
        status='waiting',
        queue_id=QUEUE_SLUG,
        conference_name=f'interaction-{CALL_SID}',
        ai_context=json.dumps({'reason': 'billing dispute', 'customer_name': 'Dana Reyes'}),
    )
    db.session.add(call)
    db.session.commit()
    return workspace, call


@pytest.fixture()
def wiring(monkeypatch):
    """Neutralise everything outside the unit: SignalWire REST, Redis, the
    queue-service dequeue, Socket.IO sleeps and emits."""
    api = FakeSignalWire()
    monkeypatch.setattr(signalwire_api, 'get_signalwire_api', lambda: api)

    slept = []
    from app import socketio
    monkeypatch.setattr(socketio, 'sleep', lambda seconds: slept.append(seconds))
    monkeypatch.setattr(socketio, 'emit', lambda *a, **k: None)

    dequeued = []
    from app.services.queue_service import QueueService
    monkeypatch.setattr(
        QueueService, 'remove_call_from_all_queues',
        lambda self, call_id: dequeued.append(call_id) or 1,
    )
    # reap_call resolves its own Redis client; it only uses it for the dequeue
    # and agent-release, both harmless with the stub above.
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: FakeQueueZset([]))

    return {'api': api, 'slept': slept, 'dequeued': dequeued}


def _run_loop(hold_app, workspace, zset, monkeypatch, interval=30):
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: zset)
    queue_dispatch._announcement_loop(
        hold_app, CALL_SID, QUEUE_SLUG, interval, workspace.id,
    )


# ---------------------------------------------------------------------------
# The setting is read at all
# ---------------------------------------------------------------------------

def test_hold_cap_reads_the_queue_setting(hold_app):
    workspace, _call = _seed(max_wait=90)
    assert queue_dispatch._queue_hold_cap_seconds(QUEUE_SLUG, workspace.id) == 90


def test_hold_cap_zero_means_disabled(hold_app):
    workspace, _call = _seed(max_wait=0)
    assert queue_dispatch._queue_hold_cap_seconds(QUEUE_SLUG, workspace.id) is None


def test_hold_cap_is_none_for_a_missing_queue(hold_app):
    workspace, _call = _seed()
    assert queue_dispatch._queue_hold_cap_seconds('nonexistent', workspace.id) is None


def test_hold_cap_does_not_read_another_workspaces_queue(hold_app):
    """Slugs repeat across workspaces; a greenlet has no request context, so
    the ORM auto-scope is inactive and the filter has to be explicit."""
    workspace, _call = _seed(max_wait=90)
    other = Workspace(name='Other tenant')
    db.session.add(other)
    db.session.flush()
    db.session.add(Queue(
        workspace_id=other.id, slug=QUEUE_SLUG, display_name='Support',
        max_wait_before_ai_fallback=15,
    ))
    db.session.commit()

    assert queue_dispatch._queue_hold_cap_seconds(QUEUE_SLUG, workspace.id) == 90
    assert queue_dispatch._queue_hold_cap_seconds(QUEUE_SLUG, other.id) == 15


def test_waited_seconds_keys_off_the_preserved_enqueue_clock():
    assert queue_dispatch._waited_seconds(json.loads(_entry(waited_seconds=140))) >= 139
    assert queue_dispatch._waited_seconds(None) is None
    assert queue_dispatch._waited_seconds({}) is None
    assert queue_dispatch._waited_seconds({'enqueued_at': 'not-a-timestamp'}) is None


# ---------------------------------------------------------------------------
# The loop terminates
# ---------------------------------------------------------------------------

def test_loop_exits_and_offers_a_callback_once_the_cap_is_exceeded(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120)
    # Enough scripted replies that an unbounded loop would keep going.
    zset = FakeQueueZset([[_entry(waited_seconds=180)] for _ in range(50)])

    _run_loop(hold_app, workspace, zset, monkeypatch)

    # It stopped on the first pass rather than draining the replies.
    assert zset.calls == 1

    callback = db.session.query(Callback).one()
    assert callback.phone_number == '+15555550123'
    assert callback.queue_id == QUEUE_SLUG
    assert callback.status == 'pending'
    # Triage context is carried forward, not overwritten by the system note.
    assert callback.reason == 'billing dispute'
    assert callback.caller_name == 'Dana Reyes'
    assert 'hold timeout' in (callback.notes or '')
    assert callback.workspace_id == workspace.id

    # The caller was told, and told the truth.
    assert len(wiring['api'].tts) == 1
    _sid, spoken = wiring['api'].tts[0]
    assert 'callback list' in spoken
    # Not the position announcement — we never say "you are number N in the
    # queue, please continue holding" on the pass that takes them off hold.
    assert 'in the queue' not in spoken

    # The line was released and the call torn down.
    assert wiring['api'].ended == [CALL_SID]
    assert CALL_SID in wiring['dequeued']
    db.session.refresh(call)
    assert call.status == 'ended'
    assert call.ended_at is not None
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED


def test_loop_keeps_announcing_position_below_the_cap(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120)
    # Pass 1: still inside the cap → position announcement. Pass 2: gone from
    # the queue (an agent took them) → the loop's original exit.
    zset = FakeQueueZset([[_entry(waited_seconds=30)], []])

    _run_loop(hold_app, workspace, zset, monkeypatch)

    assert zset.calls == 2
    assert db.session.query(Callback).count() == 0
    assert len(wiring['api'].tts) == 1
    assert 'number 1 in the queue' in wiring['api'].tts[0][1]
    assert wiring['api'].ended == []
    db.session.refresh(call)
    assert call.status == 'waiting'
    assert call.end_reason is None


def test_loop_sleep_lands_on_the_cap_instead_of_overshooting(
    hold_app, wiring, monkeypatch,
):
    workspace, _call = _seed(max_wait=45)
    zset = FakeQueueZset([[_entry(waited_seconds=30)], []])

    _run_loop(hold_app, workspace, zset, monkeypatch, interval=30)

    # Initial landing delay is clamped to the cap, then the post-announcement
    # sleep is shortened to the 15s remaining rather than another full 30s.
    assert wiring['slept'] == [30, 15]


def test_loop_gives_up_after_consecutive_tts_failures(
    hold_app, wiring, monkeypatch,
):
    """A leg that has gone away without a webhook fails every announcement;
    the loop used to log those forever."""
    workspace, _call = _seed(max_wait=0)  # cap disabled — TTS is the only exit
    wiring['api'].tts_error = RuntimeError('call not found')
    zset = FakeQueueZset([[_entry(waited_seconds=10)] for _ in range(50)])

    _run_loop(hold_app, workspace, zset, monkeypatch)

    assert zset.calls == queue_dispatch.MAX_CONSECUTIVE_TTS_FAILURES
    assert db.session.query(Callback).count() == 0


def test_loop_stops_at_the_hard_ceiling_when_the_cap_is_disabled(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=0)
    monkeypatch.setattr(queue_dispatch, 'HOLD_LOOP_CEILING_SECONDS', 60)
    zset = FakeQueueZset([[_entry(waited_seconds=10)] for _ in range(50)])

    _run_loop(hold_app, workspace, zset, monkeypatch, interval=30)

    # 30s of announcements per pass → the third pass trips the 60s ceiling.
    assert zset.calls == 3
    assert len(wiring['api'].tts) == 2
    # A disabled cap must not silently start scheduling callbacks.
    assert db.session.query(Callback).count() == 0
    db.session.refresh(call)
    assert call.status == 'waiting'


# ---------------------------------------------------------------------------
# The fallback doesn't lie and doesn't stomp on other paths
# ---------------------------------------------------------------------------

def test_fallback_loses_to_an_agent_who_just_took_the_call(
    hold_app, wiring, monkeypatch,
):
    """Push-dispatch runs in another greenlet and can claim the call between
    the cap check and the teardown. It must win."""
    workspace, call = _seed(max_wait=120)
    agent = db.session.query(User).one()
    call.assigned_agent_id = agent.id
    call.status = 'assigned'
    db.session.commit()

    queue_dispatch._offer_callback_and_release(
        CALL_SID, QUEUE_SLUG, workspace.id, 180, 120,
    )

    assert db.session.query(Callback).count() == 0
    assert wiring['api'].tts == []
    assert wiring['api'].ended == []
    db.session.refresh(call)
    assert call.status == 'assigned'
    assert call.end_reason is None


def test_fallback_does_not_promise_a_callback_without_a_dialable_number(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120, from_number=None)

    queue_dispatch._offer_callback_and_release(
        CALL_SID, QUEUE_SLUG, workspace.id, 180, 120,
    )

    assert db.session.query(Callback).count() == 0
    # Still bounded — but the announcement makes no promise it can't keep.
    _sid, spoken = wiring['api'].tts[0]
    assert 'callback' not in spoken.lower()
    assert 'try your call again' in spoken
    assert wiring['api'].ended == [CALL_SID]
    db.session.refresh(call)
    assert call.status == 'ended'


def test_fallback_reuses_a_pending_callback_instead_of_minting_a_second(
    hold_app, wiring, monkeypatch,
):
    """A returned-to-queue caller starts a fresh greenlet; one call must not
    end up with two live promises."""
    workspace, call = _seed(max_wait=120)
    existing = Callback(
        workspace_id=workspace.id,
        call_id=call.id,
        queue_id=QUEUE_SLUG,
        phone_number='+15555550123',
        caller_name='Dana Reyes',
        requested_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.session.add(existing)
    db.session.commit()

    queue_dispatch._offer_callback_and_release(
        CALL_SID, QUEUE_SLUG, workspace.id, 180, 120,
    )

    assert db.session.query(Callback).count() == 1
    # It still counts as a promise, so the caller hears the callback wording.
    assert 'callback list' in wiring['api'].tts[0][1]
    db.session.refresh(call)
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED


def test_reap_call_keeps_a_preset_end_reason(hold_app, wiring):
    """The teardown must not relabel a hold-timeout release as an abandon."""
    _workspace, call = _seed()
    call.end_reason = queue_dispatch.END_REASON_CALLBACK_SCHEDULED
    db.session.commit()

    call_watchdog.reap_call(call)

    db.session.refresh(call)
    assert call.status == 'ended'
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED
