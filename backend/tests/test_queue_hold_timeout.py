"""Hold timeout + the SWML hold cycle.

The hold experience used to be a backend greenlet pushing REST ``calling.play``
into a conference-parked leg — which this space silently ignores (HTTP 200, no
audio; proven live 2026-08-11 by the hank_hold_callback synthetic scenario).
The caller heard music and then dead air while the DB recorded a perfect flow.

Now the caller's own leg drives the hold: each cycle document plays the
position + music and ``transfer``s back to /api/queues/<slug>/hold, and
``queue_dispatch.hold_cycle_swml`` answers with the next decision. These tests
pin that state machine: announcements phrased and paced right, the
``max_wait_before_ai_fallback`` cap enforced with a durable Callback promise
the caller actually gets to HEAR before the hangup, dispatch and teardown
races resolved in the winner's favor, and the release teardown closing the row
without relabeling it.
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
BASE_URL = 'http://backend.test'


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


class FakeRedis:
    """Stands in for Redis: a static queue-zset reply plus the heartbeat set.
    ``zrange_error`` simulates a Redis outage mid-hold."""

    def __init__(self, members=None, zrange_error=None):
        self.members = list(members or [])
        self.zrange_error = zrange_error
        self.heartbeats = []

    def zrange(self, _key, _start, _stop):
        if self.zrange_error is not None:
            raise self.zrange_error
        return list(self.members)

    def set(self, key, value, ex=None):
        self.heartbeats.append((key, ex))


class FakeSignalWire:
    def __init__(self):
        self.ended = []

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
    queue-service dequeue, Socket.IO sleeps/emits, and the background-task
    spawner (captured so tests can run the teardown synchronously)."""
    api = FakeSignalWire()
    monkeypatch.setattr(signalwire_api, 'get_signalwire_api', lambda: api)

    slept = []
    spawned = []
    from app import socketio
    monkeypatch.setattr(socketio, 'sleep', lambda seconds: slept.append(seconds))
    monkeypatch.setattr(socketio, 'emit', lambda *a, **k: None)
    monkeypatch.setattr(
        socketio, 'start_background_task',
        lambda fn, *args: spawned.append((fn, args)),
    )

    dequeued = []
    from app.services.queue_service import QueueService
    monkeypatch.setattr(
        QueueService, 'remove_call_from_all_queues',
        lambda self, call_id: dequeued.append(call_id) or 1,
    )
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: FakeRedis())

    return {'api': api, 'slept': slept, 'dequeued': dequeued, 'spawned': spawned}


def _use_zset(monkeypatch, fake_redis):
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: fake_redis)
    return fake_redis


def _cycle(cycle=2):
    """One decision of the hold state machine."""
    return queue_dispatch.hold_cycle_swml(CALL_SID, QUEUE_SLUG, cycle, BASE_URL)


# --- SWML document probes ---------------------------------------------------

def _verbs(doc):
    return doc['sections']['main']


def _play_urls(doc):
    urls = []
    for verb in _verbs(doc):
        if isinstance(verb, dict) and 'play' in verb:
            p = verb['play']
            if 'urls' in p:
                urls.extend(p['urls'])
            if 'url' in p:
                urls.append(p['url'])
    return urls


def _spoken(doc):
    return ' '.join(
        u[len('say:'):] for u in _play_urls(doc)
        if isinstance(u, str) and u.startswith('say:')
    )


def _transfer_dest(doc):
    for verb in _verbs(doc):
        if isinstance(verb, dict) and 'transfer' in verb:
            return verb['transfer']['dest']
    return None


def _joins_conference(doc):
    return any(isinstance(v, dict) and 'join_conference' in v for v in _verbs(doc))


def _hangs_up(doc):
    return 'hangup' in _verbs(doc)


def _run_spawned_teardown(wiring, hold_app):
    """Execute the captured release teardown synchronously (its sleep is
    stubbed by the wiring fixture)."""
    assert wiring['spawned'], 'expected a release teardown to be scheduled'
    fn, args = wiring['spawned'][-1]
    assert fn is queue_dispatch._release_teardown
    fn(*args)


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
    """Slugs repeat across workspaces; the hold endpoint runs with no request
    context, so the ORM auto-scope is inactive and the filter must be
    explicit."""
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
# Cycle documents: what the caller hears while holding
# ---------------------------------------------------------------------------

def test_cycle_announces_position_and_keeps_holding_below_the_cap(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=30)]))

    doc = _cycle(cycle=2)

    spoken = _spoken(doc)
    assert 'number 1 in the queue' in spoken
    assert 'Please continue holding' in spoken
    # Music between announcements, then back to the hold endpoint for the
    # next decision — never a hangup mid-hold.
    assert any('hold-music' in u for u in _play_urls(doc))
    dest = _transfer_dest(doc)
    assert dest and f'/api/queues/{QUEUE_SLUG}/hold' in dest and 'n=3' in dest
    assert not _hangs_up(doc)

    assert db.session.query(Callback).count() == 0
    db.session.refresh(call)
    assert call.status == 'waiting'
    assert call.end_reason is None


def test_first_cycle_skips_the_announcement(hold_app, wiring, monkeypatch):
    """The entry greeting said the position seconds ago — cycle 1 is music
    only, so the caller doesn't hear their position twice back-to-back."""
    workspace, _call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=2)]))

    doc = _cycle(cycle=1)

    assert _spoken(doc) == ''
    assert any('hold-music' in u for u in _play_urls(doc))
    assert 'n=2' in (_transfer_dest(doc) or '')


def test_cycle_lands_on_the_cap_instead_of_overshooting(
    hold_app, wiring, monkeypatch,
):
    """When less than one full segment remains before the cap, the cycle pads
    with silence sized to the remainder so the promise fires on time."""
    workspace, _call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=100)]))

    doc = _cycle(cycle=3)

    silence = [u for u in _play_urls(doc) if str(u).startswith('silence:')]
    assert silence, f'expected a silence tail, got {_play_urls(doc)}'
    assert float(silence[0].split(':', 1)[1]) <= 20 - 5 + 0.01
    assert not any('hold-music' in str(u) for u in _play_urls(doc))
    assert _transfer_dest(doc)


def test_cycle_heartbeats_so_the_watchdog_keeps_off(hold_app, wiring, monkeypatch):
    workspace, _call = _seed(max_wait=120)
    zset = _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=30)]))

    _cycle(cycle=2)

    assert zset.heartbeats and zset.heartbeats[0][0] == f'call_heartbeat:{CALL_SID}'


def test_redis_outage_keeps_the_caller_cycling(hold_app, wiring, monkeypatch):
    """A Redis blip must read as 'unknown', not 'gone' — the caller keeps
    hearing music (no position claim we can't back) and the next fetch
    retries the decision."""
    workspace, call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis(zrange_error=RuntimeError('redis down')))

    doc = _cycle(cycle=4)

    assert _spoken(doc) == ''
    assert any('hold-music' in u for u in _play_urls(doc))
    assert _transfer_dest(doc)
    assert not _hangs_up(doc)
    db.session.refresh(call)
    assert call.status == 'waiting'


# ---------------------------------------------------------------------------
# The cap is enforced: durable promise, audible release
# ---------------------------------------------------------------------------

def test_release_claims_mints_and_promises_once_the_cap_is_exceeded(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=180)]))

    doc = _cycle(cycle=5)

    callback = db.session.query(Callback).one()
    assert callback.phone_number == '+15555550123'
    assert callback.queue_id == QUEUE_SLUG
    assert callback.status == 'pending'
    # Triage context is carried forward, not overwritten by the system note.
    assert callback.reason == 'billing dispute'
    assert callback.caller_name == 'Dana Reyes'
    assert 'hold timeout' in (callback.notes or '')
    assert callback.workspace_id == workspace.id

    # The caller is told, truthfully, in the SAME document that hangs up —
    # the promise is durable before any audio that mentions it can play.
    spoken = _spoken(doc)
    assert 'callback list' in spoken
    assert 'in the queue' not in spoken
    assert _hangs_up(doc)
    assert _transfer_dest(doc) is None

    # Dequeued before the goodbye plays; row already classified.
    assert CALL_SID in wiring['dequeued']
    db.session.refresh(call)
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED

    # The delayed teardown closes the row and keeps the classification.
    _run_spawned_teardown(wiring, hold_app)
    assert wiring['api'].ended == [CALL_SID]
    db.session.refresh(call)
    assert call.status == 'ended'
    assert call.ended_at is not None
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED


def test_release_loses_to_an_agent_who_just_took_the_call(
    hold_app, wiring, monkeypatch,
):
    """Push-dispatch runs in another greenlet and can claim the call between
    the cap check and the release. It must win — and the caller's document
    becomes the conference join, not a goodbye."""
    workspace, call = _seed(max_wait=120)
    agent = db.session.query(User).one()
    call.assigned_agent_id = agent.id
    call.status = 'assigned'
    db.session.commit()
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=180)]))

    doc = _cycle(cycle=5)

    assert _joins_conference(doc)
    assert 'joining you now' in _spoken(doc)
    assert db.session.query(Callback).count() == 0
    db.session.refresh(call)
    assert call.status == 'assigned'
    assert call.end_reason is None


def test_release_does_not_promise_a_callback_without_a_dialable_number(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120, from_number=None)
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=180)]))

    doc = _cycle(cycle=5)

    assert db.session.query(Callback).count() == 0
    # Still bounded — but the announcement makes no promise it can't keep.
    spoken = _spoken(doc)
    assert 'callback' not in spoken.lower()
    assert 'try your call again' in spoken
    assert _hangs_up(doc)

    _run_spawned_teardown(wiring, hold_app)
    db.session.refresh(call)
    assert call.status == 'ended'


def test_release_reuses_a_pending_callback_instead_of_minting_a_second(
    hold_app, wiring, monkeypatch,
):
    """A redelivered release (or a re-enqueued caller) must not end up with
    two live promises for one call."""
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
    _use_zset(monkeypatch, FakeRedis([_entry(waited_seconds=180)]))

    doc = _cycle(cycle=5)

    assert db.session.query(Callback).count() == 1
    # It still counts as a promise, so the caller hears the callback wording.
    assert 'callback list' in _spoken(doc)
    db.session.refresh(call)
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED


def test_ceiling_bounds_a_disabled_cap_without_promising(
    hold_app, wiring, monkeypatch,
):
    """An admin-disabled cap must not mean 'cycle forever' — and it must not
    silently start scheduling callbacks either."""
    workspace, call = _seed(max_wait=0)
    _use_zset(monkeypatch, FakeRedis([
        _entry(waited_seconds=queue_dispatch.HOLD_LOOP_CEILING_SECONDS + 60),
    ]))

    doc = _cycle(cycle=80)

    assert db.session.query(Callback).count() == 0
    assert 'callback' not in _spoken(doc).lower()
    assert _hangs_up(doc)
    assert CALL_SID in wiring['dequeued']


# ---------------------------------------------------------------------------
# Other decision branches
# ---------------------------------------------------------------------------

def test_dispatched_caller_is_sent_into_the_conference(
    hold_app, wiring, monkeypatch,
):
    workspace, call = _seed(max_wait=120)
    agent = db.session.query(User).one()
    call.assigned_agent_id = agent.id
    call.status = 'assigned'
    db.session.commit()
    # Dispatch dequeues, so the zset no longer holds the caller.
    _use_zset(monkeypatch, FakeRedis([]))

    doc = _cycle(cycle=3)

    assert 'joining you now' in _spoken(doc)
    assert _joins_conference(doc)
    join = next(v for v in _verbs(doc) if isinstance(v, dict) and 'join_conference' in v)
    assert join['join_conference']['name'] == f'interaction-{CALL_SID}'
    assert db.session.query(Callback).count() == 0


def test_redelivered_release_speaks_the_promise_again(
    hold_app, wiring, monkeypatch,
):
    """A crash between claim and response must not strand the caller: the
    durable state (end_reason + pending Callback) decides the wording."""
    workspace, call = _seed(max_wait=120)
    call.end_reason = queue_dispatch.END_REASON_CALLBACK_SCHEDULED
    db.session.add(Callback(
        workspace_id=workspace.id,
        call_id=call.id,
        queue_id=QUEUE_SLUG,
        phone_number='+15555550123',
        requested_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    ))
    db.session.commit()
    _use_zset(monkeypatch, FakeRedis([]))

    doc = _cycle(cycle=6)

    assert 'callback list' in _spoken(doc)
    assert _hangs_up(doc)
    assert wiring['spawned'], 'teardown must be (re)scheduled on redelivery'


def test_ended_call_gets_a_plain_hangup(hold_app, wiring, monkeypatch):
    workspace, call = _seed(max_wait=120)
    call.status = 'ended'
    call.ended_at = datetime.utcnow()
    db.session.commit()
    _use_zset(monkeypatch, FakeRedis([]))

    doc = _cycle(cycle=2)

    assert _hangs_up(doc)
    assert _spoken(doc) == ''
    assert db.session.query(Callback).count() == 0


def test_unknown_call_gets_a_plain_hangup(hold_app, wiring):
    doc = queue_dispatch.hold_cycle_swml('no-such-call', QUEUE_SLUG, 2, BASE_URL)
    assert _hangs_up(doc)


def test_vanished_queue_entry_releases_without_a_promise(
    hold_app, wiring, monkeypatch,
):
    """Not queued, not assigned, not ended: routing lost the caller. Release
    honestly instead of cycling music forever."""
    workspace, call = _seed(max_wait=120)
    _use_zset(monkeypatch, FakeRedis([]))

    doc = _cycle(cycle=4)

    spoken = _spoken(doc)
    assert 'try your call again' in spoken
    assert 'callback' not in spoken.lower()
    assert _hangs_up(doc)
    assert wiring['spawned']


# ---------------------------------------------------------------------------
# Teardown keeps the classification
# ---------------------------------------------------------------------------

def test_reap_call_keeps_a_preset_end_reason(hold_app, wiring):
    """The teardown must not relabel a hold-timeout release as an abandon."""
    _workspace, call = _seed()
    call.end_reason = queue_dispatch.END_REASON_CALLBACK_SCHEDULED
    db.session.commit()

    call_watchdog.reap_call(call)

    db.session.refresh(call)
    assert call.status == 'ended'
    assert call.end_reason == queue_dispatch.END_REASON_CALLBACK_SCHEDULED
