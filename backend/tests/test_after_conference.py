"""The after-conference decision + the return-to-queue caller experience.

The agent's conference member joins with ``end_on_exit``, so the agent
leaving ends the interaction conference and the caller's own SWML resumes.
Before 2026-08-11 the verb after ``join_conference`` was a bare ``hangup`` —
which meant return-to-queue disconnected the caller the moment the agent's
browser hung up (and the REST TTS announcement it attempted was a silent
no-op on this space, so they heard nothing first). Now that boundary is a
decision fetch, exactly like a hold-cycle iteration: ``transfer`` to
/api/queues/after-conference, answered by
``queue_dispatch.after_conference_swml``.

These tests pin that state machine: a returned caller hears the handoff
announcement and re-enters the SWML hold cycle (in the queue the return may
have retargeted), a re-taken caller joins the new agent's conference, a
normal end still hangs up, and the survival verify closes the row only when
the leg provably never re-entered the cycle. They also pin the join
documents themselves — both the entry SWML's dispatched branch and the hold
cycle's join document must end in the after-conference transfer, never an
inline hangup.

No synthetic-harness coverage exists for these flows by design: the harness
drives a caller bot only, and return-to-queue needs a human agent's WebRTC
leg to accept and then leave a conference. The SWML builders are the
testable surface, same as test_queue_hold_timeout.py.
"""

from datetime import datetime, timedelta
import json

import pytest
from flask import Flask

from app import db
from app.models import Call, Callback, Queue, User, Workspace
from app.services import queue_dispatch
from app.services import redis_service, signalwire_api


CALL_SID = 'call-after-conference-test'
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
    """Queue-zset reply + the heartbeat surface (set for writes, get for the
    survival verify's read)."""

    def __init__(self, members=None, heartbeat_value=None):
        self.members = list(members or [])
        self.heartbeat_value = heartbeat_value
        self.heartbeats = []

    def zrange(self, _key, _start, _stop):
        return list(self.members)

    def set(self, key, value, ex=None):
        self.heartbeats.append((key, ex))

    def get(self, _key):
        return self.heartbeat_value


class FakeSignalWire:
    def __init__(self):
        self.ended = []

    def end_call(self, call_sid):
        self.ended.append(call_sid)
        return {'ok': True}


def _seed(*, status='waiting', assigned=False, queue_id=QUEUE_SLUG):
    """Workspace + queue + a call in the given state. Returns (ws, call)."""
    workspace = Workspace(name='After-conference test')
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

    db.session.add(Queue(
        workspace_id=workspace.id,
        slug=QUEUE_SLUG,
        display_name='Support',
        routing_strategy='round_robin',
        max_wait_before_ai_fallback=120,
    ))
    call = Call(
        workspace_id=workspace.id,
        user_id=agent.id,
        signalwire_call_sid=CALL_SID,
        from_number='+15555550123',
        destination='+15555559999',
        destination_type='phone',
        direction='inbound',
        handler_type='human',
        status=status,
        assigned_agent_id=agent.id if assigned else None,
        queue_id=queue_id,
        conference_name=f'interaction-{CALL_SID}',
        ai_context=json.dumps({'reason': 'billing dispute'}),
    )
    db.session.add(call)
    db.session.commit()
    return workspace, call


@pytest.fixture()
def wiring(monkeypatch):
    """Neutralise everything outside the unit — same shape as the hold-cycle
    tests: SignalWire REST, Redis, the queue-service dequeue, Socket.IO."""
    api = FakeSignalWire()
    monkeypatch.setattr(signalwire_api, 'get_signalwire_api', lambda: api)

    spawned = []
    from app import socketio
    monkeypatch.setattr(socketio, 'sleep', lambda seconds: None)
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

    return {'api': api, 'dequeued': dequeued, 'spawned': spawned}


def _use_redis(monkeypatch, fake_redis):
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: fake_redis)
    return fake_redis


def _decide():
    return queue_dispatch.after_conference_swml(CALL_SID, BASE_URL)


# --- SWML document probes (same helpers as the hold-cycle tests) -----------

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


# ---------------------------------------------------------------------------
# The decision: who survives the conference ending
# ---------------------------------------------------------------------------

def test_returned_caller_hears_handoff_and_enters_hold_cycle(
    hold_app, wiring, monkeypatch,
):
    """The whole point of the endpoint: a returned-to-queue caller is
    announced to and dropped into the hold cycle instead of disconnected."""
    _ws, call = _seed(status='waiting', assigned=False)
    redis = _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    spoken = _spoken(doc)
    assert 'someone better suited' in spoken
    assert 'hold' in spoken.lower()
    dest = _transfer_dest(doc)
    assert dest and f'/api/queues/{QUEUE_SLUG}/hold' in dest and 'n=1' in dest
    assert not _hangs_up(doc)
    assert not _joins_conference(doc)
    # The fetch is the leg's proof of survival — heartbeat immediately so
    # the survival verify and the watchdog both know.
    assert redis.heartbeats and redis.heartbeats[0][0] == f'call_heartbeat:{CALL_SID}'
    db.session.refresh(call)
    assert call.status == 'waiting'


def test_returned_caller_holds_in_the_retargeted_queue(
    hold_app, wiring, monkeypatch,
):
    """Return-to-queue may retarget (target_queue_slug); the hold cycle must
    run in the call's LIVE queue, not the one it joined the conference from."""
    _ws, _call = _seed(status='waiting', assigned=False, queue_id='billing')
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    dest = _transfer_dest(doc)
    assert dest and '/api/queues/billing/hold' in dest


def test_returned_caller_without_a_queue_hangs_up(hold_app, wiring, monkeypatch):
    _ws, _call = _seed(status='waiting', assigned=False, queue_id=None)
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    assert _hangs_up(doc)
    assert _spoken(doc) == ''


def test_retaken_caller_joins_the_new_agents_conference(
    hold_app, wiring, monkeypatch,
):
    """Another agent can take the call between the return commit and the
    caller's fall-out fetch. status='assigned' means an agent is on the way
    — join the (same-named, fresh) conference, don't hold and don't die."""
    _ws, _call = _seed(status='assigned', assigned=True)
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    assert _joins_conference(doc)
    assert 'joining you now' in _spoken(doc)
    join = next(v for v in _verbs(doc) if isinstance(v, dict) and 'join_conference' in v)
    assert join['join_conference']['name'] == f'interaction-{CALL_SID}'


def test_normal_end_still_hangs_up(hold_app, wiring, monkeypatch):
    """Agent clicked End (or their browser died) on a live call: status is
    'active' with an agent assigned. The caller is done — same hangup the
    old inline verb served, and emphatically NOT a re-join into a
    conference nobody is coming back to."""
    _ws, _call = _seed(status='active', assigned=True)
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    assert _hangs_up(doc)
    assert _spoken(doc) == ''
    assert not _joins_conference(doc)


def test_terminal_call_hangs_up(hold_app, wiring, monkeypatch):
    _ws, call = _seed(status='completed')
    call.ended_at = datetime.utcnow()
    db.session.commit()
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    assert _hangs_up(doc)
    assert _spoken(doc) == ''


def test_claimed_release_hangs_up(hold_app, wiring, monkeypatch):
    """A stamped end_reason means someone (hold release, teardown) already
    owns this call's ending — never speak a queue promise over it."""
    _ws, call = _seed(status='waiting', assigned=False)
    call.end_reason = queue_dispatch.END_REASON_CALLBACK_SCHEDULED
    db.session.commit()
    _use_redis(monkeypatch, FakeRedis())

    doc = _decide()

    assert _hangs_up(doc)
    assert _spoken(doc) == ''


def test_unknown_call_hangs_up(hold_app, wiring):
    doc = queue_dispatch.after_conference_swml('no-such-call', BASE_URL)
    assert _hangs_up(doc)


# ---------------------------------------------------------------------------
# The join documents deliver the caller to the decision point
# ---------------------------------------------------------------------------

def test_hold_cycle_join_document_ends_with_the_after_conference_decision(
    hold_app, wiring, monkeypatch,
):
    """The hold cycle's dispatched branch must park the post-conference
    boundary on /after-conference — an inline hangup there is what used to
    disconnect returned callers."""
    _ws, call = _seed(status='assigned', assigned=True)
    _use_redis(monkeypatch, FakeRedis())

    doc = queue_dispatch.hold_cycle_swml(CALL_SID, QUEUE_SLUG, 3, BASE_URL)

    assert _joins_conference(doc)
    dest = _transfer_dest(doc)
    assert dest and '/api/queues/after-conference' in dest
    assert f'call_sid={CALL_SID}' in dest
    assert not _hangs_up(doc)
    # Order matters: the transfer must FOLLOW the join, so it runs when the
    # conference ends, not before the caller ever gets in.
    verbs = _verbs(doc)
    join_idx = next(i for i, v in enumerate(verbs)
                    if isinstance(v, dict) and 'join_conference' in v)
    transfer_idx = next(i for i, v in enumerate(verbs)
                        if isinstance(v, dict) and 'transfer' in v)
    assert transfer_idx == join_idx + 1


def test_entry_swml_dispatched_branch_ends_with_the_after_conference_decision(
    hold_app, wiring, monkeypatch,
):
    """Same contract for the entry SWML's immediate-dispatch branch."""
    _ws, call = _seed(status='waiting', assigned=False)
    agent = db.session.query(User).one()
    agent.signalwire_address = '/private/agent-one'
    db.session.commit()

    from app.services.queue_service import QueueService
    monkeypatch.setattr(
        QueueService, 'enqueue_call',
        lambda self, **kw: {'position': 1},
    )
    monkeypatch.setattr(
        QueueService, 'get_available_agents',
        lambda self, slug=None: [str(agent.id)],
    )
    monkeypatch.setattr(
        QueueService, 'select_agent',
        lambda self, **kw: str(agent.id),
    )
    monkeypatch.setattr(
        QueueService, 'set_agent_status',
        lambda self, *a, **kw: None,
    )
    _use_redis(monkeypatch, FakeRedis())

    doc = queue_dispatch.enqueue_and_build_swml(
        call=call,
        queue_slug=QUEUE_SLUG,
        context={},
        base_url=BASE_URL,
        start_live_transcribe=False,
    )

    assert _joins_conference(doc)
    dest = _transfer_dest(doc)
    assert dest and '/api/queues/after-conference' in dest
    assert not _hangs_up(doc)
    db.session.refresh(call)
    assert call.assigned_agent_id == agent.id  # the dispatched branch ran


# ---------------------------------------------------------------------------
# Survival verify: reap only a leg that never re-entered the cycle
# ---------------------------------------------------------------------------

def test_return_verify_reaps_a_leg_that_never_checked_in(
    hold_app, wiring, monkeypatch,
):
    """No heartbeat after the delay = the leg died with the conference and
    no call-state webhook will ever close the row. Dequeue + reap, with the
    REST end as a backstop."""
    ws, call = _seed(status='waiting', assigned=False)
    _use_redis(monkeypatch, FakeRedis(heartbeat_value=None))

    queue_dispatch._return_verify(hold_app, CALL_SID, ws.id)

    assert CALL_SID in wiring['dequeued']
    assert wiring['api'].ended == [CALL_SID]
    db.session.refresh(call)
    assert call.status == 'ended'
    assert call.ended_at is not None
    assert call.end_reason is not None


def test_return_verify_leaves_a_cycling_leg_alone(
    hold_app, wiring, monkeypatch,
):
    """A heartbeat means the /after-conference fetch (or a hold cycle)
    happened — the caller is alive and holding. Hands off."""
    ws, call = _seed(status='waiting', assigned=False)
    _use_redis(monkeypatch, FakeRedis(heartbeat_value=b'1'))

    queue_dispatch._return_verify(hold_app, CALL_SID, ws.id)

    assert wiring['dequeued'] == []
    assert wiring['api'].ended == []
    db.session.refresh(call)
    assert call.status == 'waiting'
    assert call.ended_at is None


def test_return_verify_leaves_a_progressed_call_alone(
    hold_app, wiring, monkeypatch,
):
    """Re-assigned before the verify fired — the dispatch owns the call now."""
    ws, call = _seed(status='assigned', assigned=True)
    _use_redis(monkeypatch, FakeRedis(heartbeat_value=None))

    queue_dispatch._return_verify(hold_app, CALL_SID, ws.id)

    assert wiring['dequeued'] == []
    db.session.refresh(call)
    assert call.status == 'assigned'
