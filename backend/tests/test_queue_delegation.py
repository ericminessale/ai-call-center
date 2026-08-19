"""Queue delegation: which agent gets the call, and what that implies.

The synthetic-caller harness covers the AI half of this product in depth and
the HUMAN half not at all — every scenario to date is triage -> specialist ->
hangup. Queue delegation, agent selection, the language contract and the
take-race all ran unexercised. These tests drive the real onboarding entry
point (``queue_dispatch.enqueue_and_build_swml``, which both /route and
/direct-inbound call) with seeded agents, so what is asserted is production
routing rather than a re-implementation of it.

No phone call is involved, which is the point: routing decisions are
deterministic and belong in a suite that runs in seconds, leaving live calls
for the things only a live call can prove.

The language contract is the reason this file exists. ``select_agent``
prefers agents who speak the caller's language and silently widens to the
whole pool when none do; ``conferences._maybe_start_live_translate`` starts
translation at conference join for any call flagged ``needs_translation``.
Both halves shipped. Nothing connected them, so the widening was invisible
and translation never started on the one path that needs it.
"""

from datetime import datetime

import pytest
from flask import Flask

from app import db
from app.models import Call, Queue, User, Workspace
from app.services import queue_dispatch


BASE_URL = 'http://backend.test'
QUEUE_SLUG = 'sales'


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class FakeRedis:
    """In-memory stand-in covering the operations QueueService actually uses.

    Hand-rolled rather than pulled in as a dependency: the surface is
    fourteen calls (see `grep self.redis.` in queue_service.py) and the tests
    need to reach into the state anyway to seed availability.
    """

    def __init__(self):
        self.kv = {}
        self.sets = {}
        self.zsets = {}

    # --- strings
    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, **_kwargs):
        # str(), because real Redis returns strings and callers rely on it:
        # round-robin stores index 0 and reads it back with
        # `int(raw) if raw else -1`. A real '0' is truthy; a Python 0 is not,
        # so a fake that stores ints makes round-robin re-pick agent one
        # forever — a bug in the double that looks exactly like a bug in the
        # product.
        self.kv[key] = str(value)
        return True

    def setex(self, key, _ttl, value):
        self.kv[key] = str(value)
        return True

    def exists(self, key):
        return 1 if key in self.kv or key in self.sets or key in self.zsets else 0

    def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)
            self.sets.pop(key, None)
            self.zsets.pop(key, None)
        return True

    def scan_iter(self, match=None, **_kwargs):
        prefix = (match or '*').rstrip('*')
        return [k for k in list(self.kv) + list(self.sets) + list(self.zsets)
                if k.startswith(prefix)]

    # --- sets
    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(str(v) for v in values)
        return len(values)

    def srem(self, key, *values):
        bucket = self.sets.get(key, set())
        for value in values:
            bucket.discard(str(value))
        return True

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sismember(self, key, value):
        return str(value) in self.sets.get(key, set())

    # --- sorted sets
    def zadd(self, key, mapping, **_kwargs):
        self.zsets.setdefault(key, {}).update(
            {str(k): float(v) for k, v in mapping.items()}
        )
        return len(mapping)

    def zrem(self, key, *values):
        bucket = self.zsets.get(key, {})
        for value in values:
            bucket.pop(str(value), None)
        return True

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end, withscores=False, **_kwargs):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sliced = items[start:] if end == -1 else items[start:end + 1]
        return sliced if withscores else [k for k, _ in sliced]


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(application)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def redis(monkeypatch):
    client = FakeRedis()
    # enqueue_and_build_swml imports its dependencies INSIDE the function, so
    # patching queue_dispatch's namespace does nothing — the source module is
    # what the function-local import resolves against.
    from app.services import redis_service
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: client)
    return client


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch):
    """Silence the parts that would reach a socket or the network."""
    import app as app_package
    monkeypatch.setattr(app_package.socketio, 'emit', lambda *a, **kw: None)
    # This one IS a module-level name in queue_dispatch, so patching here works.
    monkeypatch.setattr(queue_dispatch, 'emit_call_assignment_to_agent',
                        lambda **kwargs: None)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def make_workspace():
    workspace = Workspace(name='Test Co')
    db.session.add(workspace)
    db.session.commit()
    return workspace


def seed_queue(workspace, strategy='round_robin',
               language_policy='translate_now', language_wait_seconds=60):
    """Queues default to translate_now HERE so the pre-policy tests keep
    asserting what they were written for. Production defaults to
    wait_then_translate — see the policy tests below, which set it
    explicitly."""
    queue = Queue(
        workspace_id=workspace.id, slug=QUEUE_SLUG, display_name='Sales',
        routing_strategy=strategy, is_active=True,
        language_fallback_policy=language_policy,
        language_wait_seconds=language_wait_seconds,
    )
    db.session.add(queue)
    db.session.commit()
    return queue


def seed_agent(workspace, redis, name, languages, available=True):
    """A real User row, activated on the queue and marked available.

    ``signalwire_address`` matters: immediate dispatch skips any selected
    agent without one, so an agent seeded without it looks available and is
    never actually assigned — a silent no-op that would make these tests lie.
    """
    agent = User(
        email=f'{name}@test.co', name=name, workspace_id=workspace.id,
        role='agent', is_active=True, languages=languages,
        signalwire_address=f'/private/{name}',
    )
    agent.set_password('x')
    db.session.add(agent)
    db.session.commit()

    if available:
        redis.sadd('agents:available', str(agent.id))
        from app.services.queue_service import QueueService
        qs = QueueService(redis, workspace_id=workspace.id)
        redis.sadd(qs._ws_agents_key(QUEUE_SLUG), str(agent.id))
    return agent


def arrive(workspace, *, caller_language='en-US', agents=(), strategy='round_robin',
           sid='call-delegation-1'):
    """Drive the real queue-onboarding path for one inbound call."""
    # calls.user_id is NOT NULL — every call is owned by a workspace user,
    # separately from the agent it later gets assigned to.
    owner = User.query.filter_by(email='owner@test.co').first()
    if owner is None:
        owner = User(email='owner@test.co', name='owner',
                     workspace_id=workspace.id, role='admin', is_active=True)
        owner.set_password('x')
        db.session.add(owner)
        db.session.commit()

    call = Call(
        signalwire_call_sid=sid, workspace_id=workspace.id, user_id=owner.id,
        from_number='+15551230000', destination='+15559990000',
        destination_type='phone',
        direction='inbound', status='waiting', caller_language=caller_language,
        created_at=datetime.utcnow(),
    )
    db.session.add(call)
    db.session.commit()

    agent_languages = {str(a.id): (a.languages or []) for a in agents}
    queue_dispatch.enqueue_and_build_swml(
        call=call, queue_slug=QUEUE_SLUG, context={}, base_url=BASE_URL,
        routing_strategy=strategy, caller_language=caller_language,
        agent_languages=agent_languages, skill_levels={}, priority=5,
        start_live_transcribe=False,
    )
    db.session.refresh(call)
    return call


# ---------------------------------------------------------------------------
# Language-aware delegation
# ---------------------------------------------------------------------------

def test_a_spanish_caller_reaches_the_spanish_speaking_agent(app, redis):
    """Language preference runs BEFORE the routing strategy, so it has to win
    against a strategy that would otherwise pick the other agent."""
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])
    spanish = seed_agent(workspace, redis, 'ana', ['es-ES', 'en-US'])

    call = arrive(workspace, caller_language='es-ES', agents=[english, spanish])

    assert call.assigned_agent_id == spanish.id
    # A match is not a fallback: nothing to translate.
    assert call.needs_translation is False


def test_a_language_fallback_flags_the_call_for_translation(app, redis):
    """The gap this file was written for.

    With nobody who speaks the caller's language, selection widens to the
    whole pool — correct, because waiting for a match may mean waiting
    forever. But the widening was never recorded, so
    conferences._maybe_start_live_translate (which reads this exact flag, and
    has shipped for weeks) could not fire on the only path that needs it.
    """
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='es-ES', agents=[english])

    assert call.assigned_agent_id == english.id, 'the caller must still connect'
    assert call.needs_translation is True


def test_an_english_caller_never_triggers_translation(app, redis):
    """Guard against flagging everything: the common case must stay clean."""
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='en-US', agents=[english])

    assert call.assigned_agent_id == english.id
    assert call.needs_translation is False


def test_an_unknown_agent_language_is_not_treated_as_a_mismatch(app, redis):
    """No declared languages means no evidence, and guessing wrong here starts
    a paid translation stream on a call that never needed one."""
    workspace = make_workspace()
    seed_queue(workspace)
    agent = User(
        email='nolang@test.co', name='nolang', workspace_id=workspace.id,
        role='agent', is_active=True, languages=[],
        signalwire_address='/private/nolang',
    )
    agent.set_password('x')
    db.session.add(agent)
    db.session.commit()
    redis.sadd('agents:available', str(agent.id))
    from app.services.queue_service import QueueService
    qs = QueueService(redis, workspace_id=workspace.id)
    redis.sadd(qs._ws_agents_key(QUEUE_SLUG), str(agent.id))

    call = arrive(workspace, caller_language='es-ES', agents=[agent])

    assert call.assigned_agent_id == agent.id
    assert call.needs_translation is False


# ---------------------------------------------------------------------------
# Delegation basics that had no coverage
# ---------------------------------------------------------------------------

def test_a_call_with_no_available_agents_stays_queued(app, redis):
    workspace = make_workspace()
    seed_queue(workspace)
    seed_agent(workspace, redis, 'ed', ['en-US'], available=False)

    call = arrive(workspace, caller_language='en-US')

    assert call.assigned_agent_id is None
    assert call.status == 'waiting'


def test_an_agent_who_never_activated_the_queue_is_not_a_candidate(app, redis):
    """Availability is not consent: the activation set is the contract, and
    ignoring it hands agents calls for queues they never opted into."""
    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    from app.services.queue_service import QueueService
    qs = QueueService(redis, workspace_id=workspace.id)
    redis.srem(qs._ws_agents_key(QUEUE_SLUG), str(agent.id))

    call = arrive(workspace, caller_language='en-US', agents=[agent])

    assert call.assigned_agent_id is None


def test_a_second_dispatch_cannot_steal_an_assigned_call(app, redis):
    """The atomic claim. Two inbound calls arriving while several agents are
    free run this path concurrently; check-then-act let both write."""
    workspace = make_workspace()
    seed_queue(workspace)
    first = seed_agent(workspace, redis, 'ed', ['en-US'])
    seed_agent(workspace, redis, 'ana', ['en-US'])

    call = arrive(workspace, caller_language='en-US', agents=[first])
    owner = call.assigned_agent_id
    assert owner is not None

    # Re-run onboarding for the SAME call, as a duplicate webhook would.
    agent_languages = {str(first.id): ['en-US']}
    queue_dispatch.enqueue_and_build_swml(
        call=call, queue_slug=QUEUE_SLUG, context={}, base_url=BASE_URL,
        routing_strategy='round_robin', caller_language='en-US',
        agent_languages=agent_languages, skill_levels={}, priority=5,
        start_live_transcribe=False,
    )
    db.session.refresh(call)

    assert call.assigned_agent_id == owner, 'ownership must not move'


# ---------------------------------------------------------------------------
# The paths the first version of this fix missed.
# ---------------------------------------------------------------------------

def test_the_flag_reads_the_agent_row_not_the_supplied_map(app, redis):
    """Production builds the language map with
    QueueService.get_languages_for_agents, which substitutes ['en-US'] for an
    agent who declared nothing. Taking that map at face value reads undeclared
    as an explicit English declaration and starts a paid translation stream on
    no evidence. The first version of this test built the map by hand and so
    never saw it.
    """
    workspace = make_workspace()
    seed_queue(workspace)
    agent = User(
        email='undeclared@test.co', name='undeclared', workspace_id=workspace.id,
        role='agent', is_active=True, languages=[],
        signalwire_address='/private/undeclared',
    )
    agent.set_password('x')
    db.session.add(agent)
    db.session.commit()
    redis.sadd('agents:available', str(agent.id))
    from app.services.queue_service import QueueService
    qs = QueueService(redis, workspace_id=workspace.id)
    redis.sadd(qs._ws_agents_key(QUEUE_SLUG), str(agent.id))

    # Exactly what production would pass: [] widened to ['en-US'].
    assert qs.get_languages_for_agents([str(agent.id)]) == {str(agent.id): ['en-US']}

    call = Call(
        signalwire_call_sid='call-map-fidelity', workspace_id=workspace.id,
        user_id=agent.id, from_number='+15551230000',
        destination='+15559990000', destination_type='phone',
        direction='inbound', status='waiting', caller_language='es-ES',
        created_at=datetime.utcnow(),
    )
    db.session.add(call)
    db.session.commit()

    queue_dispatch.enqueue_and_build_swml(
        call=call, queue_slug=QUEUE_SLUG, context={}, base_url=BASE_URL,
        routing_strategy='round_robin', caller_language='es-ES',
        agent_languages=qs.get_languages_for_agents([str(agent.id)]),
        skill_levels={}, priority=5, start_live_transcribe=False,
    )
    db.session.refresh(call)

    assert call.assigned_agent_id == agent.id
    assert call.needs_translation is False, 'undeclared is not English'


def test_direct_inbound_flags_without_being_handed_a_language_map(app, redis):
    """/direct-inbound calls build_ingress_swml with no agent_languages at all,
    so a version of this that trusted the map could never flag anything on the
    path a PSTN caller actually arrives on."""
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = Call(
        signalwire_call_sid='call-direct-inbound', workspace_id=workspace.id,
        user_id=english.id, from_number='+15551230000',
        destination='+15559990000', destination_type='phone',
        direction='inbound', status='waiting', caller_language='es-ES',
        created_at=datetime.utcnow(),
    )
    db.session.add(call)
    db.session.commit()

    queue_dispatch.enqueue_and_build_swml(
        call=call, queue_slug=QUEUE_SLUG, context={}, base_url=BASE_URL,
        routing_strategy='round_robin', caller_language='es-ES',
        agent_languages=None,           # the direct-inbound shape
        skill_levels={}, priority=5, start_live_transcribe=False,
    )
    db.session.refresh(call)

    assert call.assigned_agent_id == english.id
    assert call.needs_translation is True


def test_a_caller_who_waits_is_flagged_when_an_agent_finally_takes_it(app, redis):
    """The delayed path, and the one that matters most: waiting happens
    precisely BECAUSE no language-matched agent was free, so the caller who
    waits is more likely to need translation, not less."""
    from app.services.call_language import flag_translation_if_mismatched

    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'], available=False)

    call = arrive(workspace, caller_language='es-ES', sid='call-waited')
    assert call.assigned_agent_id is None, 'nobody was available at arrival'
    assert call.needs_translation is False

    # ...an agent goes available later and push-dispatch claims the call.
    flagged = flag_translation_if_mismatched(call, english)
    db.session.commit()
    db.session.refresh(call)

    assert flagged is True
    assert call.needs_translation is True


def test_the_flag_is_not_set_twice(app, redis):
    """Idempotent: both claim points can run for one call across a
    return-to-queue, and re-flagging should be a no-op rather than churn."""
    from app.services.call_language import flag_translation_if_mismatched

    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='es-ES', agents=[english])
    assert call.needs_translation is True

    assert flag_translation_if_mismatched(call, english) is False


# ---------------------------------------------------------------------------
# Routing strategies — the selection rules themselves.
# ---------------------------------------------------------------------------

def _qs(workspace, redis):
    from app.services.queue_service import QueueService
    return QueueService(redis, workspace_id=workspace.id)


def test_round_robin_moves_on_instead_of_re_picking_the_same_agent(app, redis):
    """Otherwise the first agent alphabetically takes every call and the rest
    of the floor sits idle — the failure mode round-robin exists to prevent."""
    workspace = make_workspace()
    seed_queue(workspace)
    qs = _qs(workspace, redis)

    picks = [
        qs.select_agent(queue_slug=QUEUE_SLUG, routing_strategy='round_robin',
                        available_agents=['1', '2', '3'])
        for _ in range(3)
    ]

    assert len(set(picks)) == 3, f'expected each agent once, got {picks}'


def test_skill_based_routing_prefers_the_more_skilled_agent(app, redis):
    workspace = make_workspace()
    seed_queue(workspace, strategy='skill_based')
    qs = _qs(workspace, redis)

    picked = qs.select_agent(
        queue_slug=QUEUE_SLUG, routing_strategy='skill_based',
        available_agents=['1', '2', '3'],
        skill_levels={'1': 2, '2': 9, '3': 5},
    )

    assert picked == '2'


def test_language_preference_outranks_the_routing_strategy(app, redis):
    """Language runs BEFORE the strategy. A skill rule that would otherwise
    pick the English expert must not beat the only Spanish speaker."""
    workspace = make_workspace()
    seed_queue(workspace, strategy='skill_based')
    qs = _qs(workspace, redis)

    picked = qs.select_agent(
        queue_slug=QUEUE_SLUG, routing_strategy='skill_based',
        available_agents=['1', '2'],
        skill_levels={'1': 10, '2': 1},
        caller_language='es-ES',
        agent_languages={'1': ['en-US'], '2': ['es-ES']},
    )

    assert picked == '2', 'the Spanish speaker must win despite lower skill'


def test_no_available_agents_selects_nobody(app, redis):
    workspace = make_workspace()
    seed_queue(workspace)

    assert _qs(workspace, redis).select_agent(
        queue_slug=QUEUE_SLUG, routing_strategy='round_robin',
        available_agents=[],
    ) is None


# ---------------------------------------------------------------------------
# Return to queue — freeing the agent.
# ---------------------------------------------------------------------------

def _returned_call(workspace, owner, sid, status='waiting', agent_id=None):
    call = Call(
        signalwire_call_sid=sid, workspace_id=workspace.id, user_id=owner.id,
        from_number='+15551230000', destination='+15559990000',
        destination_type='phone', direction='inbound', status=status,
        assigned_agent_id=agent_id, created_at=datetime.utcnow(),
    )
    db.session.add(call)
    db.session.commit()
    return call


def test_a_drifted_redis_status_still_frees_the_agent(app, redis):
    """The stuck-busy bug. Freeing used to require Redis still tracking the
    same call, so any drift — a missed write, a restart, a takeover — left the
    agent busy with no call, invisible to dispatch for the rest of the shift.
    """
    from app.api.call_control import release_agent_after_return

    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    qs = _qs(workspace, redis)
    # Tracks a call that no longer exists anywhere.
    qs.set_agent_status(str(agent.id), 'busy', current_call_id='ghost-call')

    freed = release_agent_after_return(qs, agent.id, 'call-being-returned')

    assert freed is True
    assert qs.get_agent_status(str(agent.id))['status'] == 'available'


def test_an_agent_already_on_a_newer_call_is_left_busy(app, redis):
    """The opposite error, and why freeing unconditionally is wrong: between
    two overlapping returns the agent can already have been dispatched a new
    call. Clearing that hands them a second one."""
    from app.api.call_control import release_agent_after_return

    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    owner = User.query.filter_by(role='admin').first() or agent
    live = _returned_call(workspace, owner, 'newer-live-call',
                          status='active', agent_id=agent.id)
    qs = _qs(workspace, redis)
    qs.set_agent_status(str(agent.id), 'busy', current_call_id=live.signalwire_call_sid)

    freed = release_agent_after_return(qs, agent.id, 'the-older-returned-call')

    assert freed is False
    assert qs.get_agent_status(str(agent.id))['status'] == 'busy'


def test_a_tracked_call_that_has_ended_is_not_protection(app, redis):
    """Only a LIVE assignment outranks the release; an ended call tracked in
    Redis is drift by another name."""
    from app.api.call_control import release_agent_after_return

    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    ended = _returned_call(workspace, agent, 'finished-call',
                           status='ended', agent_id=agent.id)
    ended.ended_at = datetime.utcnow()
    db.session.commit()
    qs = _qs(workspace, redis)
    qs.set_agent_status(str(agent.id), 'busy', current_call_id=ended.signalwire_call_sid)

    assert release_agent_after_return(qs, agent.id, 'other-call') is True


def test_a_call_tracked_for_a_different_agent_does_not_block_the_release(app, redis):
    from app.api.call_control import release_agent_after_return

    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    other_agent = seed_agent(workspace, redis, 'ana', ['en-US'])
    someone_elses = _returned_call(workspace, agent, 'someone-elses-call',
                                   status='active', agent_id=other_agent.id)
    qs = _qs(workspace, redis)
    qs.set_agent_status(str(agent.id), 'busy',
                        current_call_id=someone_elses.signalwire_call_sid)

    assert release_agent_after_return(qs, agent.id, 'my-returned-call') is True


def test_returning_someone_elses_call_is_refused(app, redis):
    """Every teardown step in the endpoint is written in terms of the
    REQUESTER — their CallLeg, their conference participant, an SDK-hangup to
    their browser. A supervisor returning another agent's call therefore tore
    down nothing and left that agent connected; once the release correctly
    targeted the assigned agent, they would also be marked available and
    handed a second call mid-conversation.

    Refusing is the honest contract until an agent-side teardown channel
    exists. This asserts the guard, and names why it cannot simply be
    deleted.
    """
    import inspect
    from app.api import call_control

    source = inspect.getsource(call_control.return_call_to_queue)
    assert "handler_id != request.current_user.id" in source
    assert "403" in source
    # The teardown that motivates the guard must still be requester-scoped;
    # if that ever changes, this guard can be revisited deliberately.
    assert "_find_agent_participant(call, user.id)" in source



def test_the_handler_of_a_taken_over_call_is_still_resolvable(app, redis):
    """assigned_agent_id is NULL after a human takes a call over from the AI —
    the handler lives on an active human CallLeg and call.user_id. A guard
    keyed only on assigned_agent_id skipped exactly those calls, letting
    another supervisor requeue a caller out from under the agent talking to
    them."""
    from app.api.call_control import resolve_call_handler_id
    from app.models import CallLeg

    workspace = make_workspace()
    seed_queue(workspace)
    owner = seed_agent(workspace, redis, 'owner', ['en-US'])
    taker = seed_agent(workspace, redis, 'taker', ['en-US'])

    call = _returned_call(workspace, owner, 'taken-over-call', status='active')
    assert call.assigned_agent_id is None

    db.session.add(CallLeg(
        call_id=call.id, workspace_id=workspace.id, user_id=taker.id,
        leg_type='human_agent', leg_number=2, status='active',
        transition_reason='takeover',
    ))
    db.session.commit()

    assert resolve_call_handler_id(call) == taker.id


def test_the_assigned_agent_still_wins_when_present(app, redis):
    from app.api.call_control import resolve_call_handler_id

    workspace = make_workspace()
    seed_queue(workspace)
    owner = seed_agent(workspace, redis, 'owner', ['en-US'])
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    call = _returned_call(workspace, owner, 'plain-call', status='active',
                          agent_id=agent.id)

    assert resolve_call_handler_id(call) == agent.id


def test_an_ended_human_leg_does_not_claim_the_call(app, redis):
    """A completed takeover leg is history, not the current handler."""
    from app.api.call_control import resolve_call_handler_id
    from app.models import CallLeg

    workspace = make_workspace()
    seed_queue(workspace)
    owner = seed_agent(workspace, redis, 'owner', ['en-US'])
    gone = seed_agent(workspace, redis, 'gone', ['en-US'])
    call = _returned_call(workspace, owner, 'stale-leg-call', status='active')

    db.session.add(CallLeg(
        call_id=call.id, workspace_id=workspace.id, user_id=gone.id,
        leg_type='human_agent', leg_number=2, status='completed',
        ended_at=datetime.utcnow(),
    ))
    db.session.commit()

    assert resolve_call_handler_id(call) == owner.id


# ---------------------------------------------------------------------------
# Telling the caller. The delay translation adds is indistinguishable from a
# broken line unless somebody explains it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code,expected', [
    ('es-ES', 'traducirá'),
    ('es', 'traducirá'),
    ('fr-FR', 'traduit'),
    ('en-US', 'translated'),
    ('de-DE', 'translated'),   # unsupported -> English beats silence
    (None, 'translated'),
])
def test_the_notice_is_spoken_in_the_callers_language(code, expected):
    """Announcing a translated line in English to a Spanish speaker tells the
    one person in the conversation who cannot read it."""
    from app.services.call_language import translation_notice

    assert expected in translation_notice(code)


def test_the_notice_says_it_is_a_real_person():
    """Three things a caller needs, or they hang up on a working call: that
    translation is happening, that the pause is expected, and that the other
    party is human."""
    from app.services.call_language import translation_notice

    english = translation_notice('en-US')
    assert 'translated' in english
    assert 'pause' in english
    assert 'real person' in english


def _greeting_of(document):
    for verb in document['sections']['main']:
        if 'play' in verb and 'url' in verb['play']:
            return verb['play']['url']
    return None


def test_a_translated_connection_greets_the_caller_about_it(app, redis):
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = Call(
        signalwire_call_sid='call-notice', workspace_id=workspace.id,
        user_id=english.id, from_number='+15551230000',
        destination='+15559990000', destination_type='phone',
        direction='inbound', status='waiting', caller_language='es-ES',
        created_at=datetime.utcnow(),
    )
    db.session.add(call)
    db.session.commit()

    document = queue_dispatch.enqueue_and_build_swml(
        call=call, queue_slug=QUEUE_SLUG, context={}, base_url=BASE_URL,
        routing_strategy='round_robin', caller_language='es-ES',
        agent_languages=None, skill_levels={}, priority=5,
        start_live_transcribe=False,
    )
    db.session.refresh(call)

    assert call.needs_translation is True
    greeting = _greeting_of(document)
    assert greeting is not None and greeting.startswith('say:')
    assert 'traducirá' in greeting, 'the notice must be in the caller\'s language'


def test_an_ordinary_connection_keeps_the_ordinary_greeting(app, redis):
    """No notice when there is nothing to explain — a translation warning on a
    same-language call is just noise that makes the caller doubt the line."""
    workspace = make_workspace()
    seed_queue(workspace)
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='en-US', agents=[english],
                  sid='call-plain-greeting')

    assert call.needs_translation is False


def test_a_caller_who_waited_also_hears_the_notice(app, redis):
    """The delayed path deserves it MORE than the immediate one: holding is
    what happens because nobody who speaks the caller's language was free, so
    a caller who waited is the most likely of all to be connected across a
    language gap. The first version of this notice only covered immediate
    dispatch — the same delayed-path miss as the flag itself."""
    workspace = make_workspace()
    seed_queue(workspace)
    owner = seed_agent(workspace, redis, 'ed', ['en-US'], available=False)

    call = _returned_call(workspace, owner, 'waited-then-joined', status='waiting')
    call.caller_language = 'es-ES'
    call.needs_translation = True
    call.conference_name = 'interaction-waited-then-joined'
    db.session.commit()

    document = queue_dispatch._join_conference_swml(call, BASE_URL)

    assert 'traducirá' in _greeting_of(document)


def test_the_ordinary_join_keeps_the_ordinary_announcement(app, redis):
    workspace = make_workspace()
    seed_queue(workspace)
    owner = seed_agent(workspace, redis, 'ed', ['en-US'], available=False)

    call = _returned_call(workspace, owner, 'plain-join', status='waiting')
    call.conference_name = 'interaction-plain-join'
    db.session.commit()

    assert 'An agent is joining you now' in _greeting_of(
        queue_dispatch._join_conference_swml(call, BASE_URL)
    )


def test_auto_started_translation_is_readable_by_the_status_endpoint(app, redis):
    """The auto-start and the manual agent button write the same Redis key, so
    they must write the same payload — /translate/status reads from_lang and
    to_lang, and a different shape reports translation active with both
    languages null."""
    import inspect
    from app.api import conferences, call_control

    auto = inspect.getsource(conferences._maybe_start_live_translate)
    manual = inspect.getsource(call_control.start_translate)
    status = inspect.getsource(call_control.translate_status)

    assert "'from_lang': from_lang, 'to_lang': to_lang" in auto
    assert "'from_lang': from_lang, 'to_lang': to_lang" in manual
    assert 'from_lang' in status


def test_the_translation_marker_outlives_the_call(app, redis):
    """The marker is what /translate/status reports and what the restart path
    checks for. Expiring mid-call makes an actively translating call read as
    inactive, and the restart then skips the stop-before-start that
    live_translate requires."""
    from app.api.call_control import TRANSLATE_STATE_TTL_SECONDS
    from app.services import call_watchdog

    assert TRANSLATE_STATE_TTL_SECONDS >= 4 * 60 * 60
    # Both writers use the shared constant rather than a literal.
    import inspect
    from app.api import conferences, call_control
    assert 'TRANSLATE_STATE_TTL_SECONDS' in inspect.getsource(
        conferences._maybe_start_live_translate)
    assert 'TRANSLATE_STATE_TTL_SECONDS' in inspect.getsource(
        call_control.start_translate)


# ---------------------------------------------------------------------------
# language_fallback_policy — what happens when nobody speaks their language.
# ---------------------------------------------------------------------------

def test_translate_now_connects_immediately_and_flags(app, redis):
    workspace = make_workspace()
    seed_queue(workspace, language_policy='translate_now')
    english = seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='es-ES', agents=[english],
                  sid='policy-translate-now')

    assert call.assigned_agent_id == english.id
    assert call.needs_translation is True


def test_wait_only_leaves_the_caller_queued_for_a_real_speaker(app, redis):
    """The caller is not stranded: the queue's existing hold cap still runs,
    so 'wait' resolves into a callback rather than forever."""
    workspace = make_workspace()
    seed_queue(workspace, language_policy='wait_only')
    seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='es-ES', sid='policy-wait-only')

    assert call.assigned_agent_id is None
    assert call.status == 'waiting'
    assert call.needs_translation is False


def test_wait_only_still_connects_a_matching_agent(app, redis):
    """Holding out is about the MISMATCH, not about queuing everyone."""
    workspace = make_workspace()
    seed_queue(workspace, language_policy='wait_only')
    spanish = seed_agent(workspace, redis, 'ana', ['es-ES'])

    call = arrive(workspace, caller_language='es-ES', agents=[spanish],
                  sid='policy-wait-only-match')

    assert call.assigned_agent_id == spanish.id
    assert call.needs_translation is False


def test_wait_then_translate_holds_out_at_arrival(app, redis):
    """A caller who has just arrived has waited zero seconds, so the default
    policy gives the floor a chance to free up a real speaker first."""
    workspace = make_workspace()
    seed_queue(workspace, language_policy='wait_then_translate',
               language_wait_seconds=60)
    seed_agent(workspace, redis, 'ed', ['en-US'])

    call = arrive(workspace, caller_language='es-ES', sid='policy-wait-then')

    assert call.assigned_agent_id is None


@pytest.mark.parametrize('policy,waited,expected', [
    ('translate_now', 0, True),
    ('wait_only', 0, False),
    ('wait_only', 9999, False),
    ('wait_then_translate', 0, False),
    ('wait_then_translate', 59, False),
    ('wait_then_translate', 60, True),
    ('wait_then_translate', 120, True),
    # Unimplemented policy degrades to the sane one rather than meaning
    # "never connect".
    ('ask_caller', 0, False),
    ('ask_caller', 120, True),
])
def test_the_policy_decision_table(app, policy, waited, expected):
    from app.services.call_language import language_fallback_allowed

    queue = Queue(slug='x', display_name='X', workspace_id=1,
                  language_fallback_policy=policy, language_wait_seconds=60)

    assert language_fallback_allowed(queue, waited_seconds=waited) is expected


def test_a_missing_queue_row_still_connects_the_caller(app):
    """Policy is a refinement, not a prerequisite. A call routed to a queue
    with no row (or before the column existed) must not be held forever."""
    from app.services.call_language import language_fallback_allowed

    assert language_fallback_allowed(None, waited_seconds=0) is True


# ---------------------------------------------------------------------------
# The policy has to be reachable, cloneable, and transport-independent.
# ---------------------------------------------------------------------------

def test_the_admin_api_can_actually_set_the_policy():
    """A tunable nobody can tune is a hardcoded default with extra steps: the
    create handler ignored both fields and the update handler's writable list
    omitted them, so every queue was stuck on whatever the migration set."""
    import inspect
    from app.api import admin

    create = inspect.getsource(admin.create_queue)
    update = inspect.getsource(admin.update_queue)

    assert 'language_fallback_policy=language_policy' in create
    assert 'LANGUAGE_FALLBACK_POLICIES' in create, 'unknown policies must 400'
    assert "'language_fallback_policy', 'language_wait_seconds'" in update
    assert 'must be between 0 and 3600' in update


def test_workspace_provisioning_clones_the_policy():
    """Cloned queues that quietly revert to the default make a provisioned
    workspace behave differently from the template it came from."""
    import inspect
    from app.services import workspace_provision

    source = inspect.getsource(workspace_provision)
    assert 'language_fallback_policy=q.language_fallback_policy' in source
    assert 'language_wait_seconds=q.language_wait_seconds' in source


def test_bridge_transport_does_not_pretend_to_enforce_the_policy():
    """Bridge parks callers in SignalWire's NATIVE queue and connects an
    agent by dialing them into queue:<slug>, which pops the OLDEST caller — we
    never choose the pairing. A check against the arriving call approves an
    agent for one caller and hands them another, so an attempt at enforcement
    here is not a partial win, it is a wrong answer delivered confidently.

    The honest contract is a loud warning and translate_now behaviour.
    """
    import inspect
    from app.services.call_transport import bridge

    source = inspect.getsource(bridge)
    assert 'allow_language_fallback' not in source, (
        'bridge cannot honour the policy; claiming to is worse than not'
    )
    assert 'cannot honour' in source, 'the limitation must be logged, not hidden'


def test_push_dispatch_looks_past_a_call_it_cannot_take(app, redis):
    """Head-of-line blocking: one caller the agent cannot take must not park
    everyone queued behind them."""
    import inspect
    from app.services import queue_service as qs_module

    source = inspect.getsource(qs_module.QueueService._push_dispatch_waiting_call)
    assert 'PUSH_DISPATCH_SCAN_DEPTH' in source
    assert qs_module.PUSH_DISPATCH_SCAN_DEPTH > 1
    # Eligibility is decided per candidate inside the scan, not after it.
    assert '_agent_may_take_call' in source
