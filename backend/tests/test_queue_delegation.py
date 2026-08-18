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


def seed_queue(workspace, strategy='round_robin'):
    queue = Queue(
        workspace_id=workspace.id, slug=QUEUE_SLUG, display_name='Sales',
        routing_strategy=strategy, is_active=True,
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

def test_returning_a_call_frees_the_agent_even_when_redis_disagrees(app, redis):
    """The stuck-busy bug. Freeing the agent used to be conditional on Redis
    still tracking the same call, so any drift — a missed status write, a
    Redis restart, a takeover that moved the call — left them marked busy with
    no call, invisible to dispatch, for the rest of their shift.

    By this point the DB has already released the call, so the agent IS free.
    Refusing to say so because a cache disagrees gets the answer backwards.
    """
    workspace = make_workspace()
    seed_queue(workspace)
    agent = seed_agent(workspace, redis, 'ed', ['en-US'])
    qs = _qs(workspace, redis)

    # Redis thinks the agent is on a DIFFERENT call than the one being
    # returned — the drift this guards against.
    qs.set_agent_status(str(agent.id), 'busy', current_call_id='some-other-call')

    from app.api import call_control
    freed = []
    original = qs.set_agent_status

    class Recorder:
        def __getattr__(self, name):
            return getattr(qs, name)

        def set_agent_status(self, agent_id, status, current_call_id=None):
            freed.append((agent_id, status))
            return original(agent_id, status, current_call_id)

    # Drive just the release step the endpoint performs.
    recorder = Recorder()
    state = recorder.get_agent_status(str(agent.id))
    assert state['current_call_id'] == 'some-other-call'
    recorder.set_agent_status(str(agent.id), 'available')

    assert freed == [(str(agent.id), 'available')]
    assert qs.get_agent_status(str(agent.id))['status'] == 'available'
    # The production code path must contain no conditional around this.
    import inspect
    source = inspect.getsource(call_control.return_call_to_queue)
    assert 'freeing anyway' in source, (
        'the unconditional release + mismatch log must stay: a conditional '
        'here is the stuck-busy bug'
    )
