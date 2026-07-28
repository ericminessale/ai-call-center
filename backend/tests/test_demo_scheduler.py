"""In-process workspace GC scheduler (HIGH-7).

The behaviours that matter here are the refusals, because the whole point is a
reaper that runs without the operator-only cron container while staying
completely absent from a clone-and-own install.
"""
import app.services.demo_scheduler as sched


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------

def test_enabled_by_default(monkeypatch):
    """Default-on: a silent 503 for every visitor is worse than a redundant
    sweep for an operator still running the cron container."""
    monkeypatch.delenv('WORKSPACE_GC_IN_PROCESS', raising=False)
    assert sched.in_process_gc_enabled()


def test_only_the_literal_false_disables_it(monkeypatch):
    monkeypatch.setenv('WORKSPACE_GC_IN_PROCESS', 'FALSE')
    assert not sched.in_process_gc_enabled()
    monkeypatch.setenv('WORKSPACE_GC_IN_PROCESS', 'no')
    assert sched.in_process_gc_enabled(), 'only "false" should disable'


def test_interval_defaults_to_the_cron_cadence(monkeypatch):
    monkeypatch.delenv('WORKSPACE_GC_INTERVAL_SECONDS', raising=False)
    assert sched.gc_interval_seconds() == 3600


def test_interval_is_clamped_and_garbage_tolerant(monkeypatch):
    for raw, want in (('0', 60), ('-5', 60), ('999999', 86400), ('abc', 3600), ('120', 120)):
        monkeypatch.setenv('WORKSPACE_GC_INTERVAL_SECONDS', raw)
        assert sched.gc_interval_seconds() == want, raw


def test_lock_ttl_outlives_a_tick(monkeypatch):
    """The lock has to survive a full sweep or another worker steals the loop."""
    monkeypatch.setenv('WORKSPACE_GC_INTERVAL_SECONDS', '600')
    assert sched._lock_ttl() > sched.gc_interval_seconds()


# ---------------------------------------------------------------------------
# start() refusals — the clone-and-own guarantee
# ---------------------------------------------------------------------------

class _SpyRedis:
    """Minimal Redis stand-in with real SET NX / GET semantics."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.sets = []

    def set(self, key, value, **kw):
        self.sets.append((key, value, kw))
        if kw.get('nx') and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


def _patch(monkeypatch, *, tenancy, redis=None, spawned=None):
    monkeypatch.setattr(
        'app.utils.demo_config.tenancy_mode_active', lambda: tenancy, raising=False,
    )
    monkeypatch.setattr(
        'app.services.redis_service.get_redis_client', lambda: redis, raising=False,
    )
    import app as app_pkg
    monkeypatch.setattr(
        app_pkg.socketio, 'start_background_task',
        lambda *a, **k: spawned.append(a), raising=False,
    )


def test_never_starts_on_a_clone_and_own_install(monkeypatch):
    """No visitor workspaces exist there and the install's single workspace must
    never be GC'd — the loop must not even spawn."""
    spawned = []
    _patch(monkeypatch, tenancy=False, redis=_SpyRedis(), spawned=spawned)
    sched.start(object())
    assert spawned == []


def test_does_not_start_when_disabled(monkeypatch):
    spawned = []
    monkeypatch.setenv('WORKSPACE_GC_IN_PROCESS', 'false')
    _patch(monkeypatch, tenancy=True, redis=_SpyRedis(), spawned=spawned)
    sched.start(object())
    assert spawned == []


def test_starts_in_hosted_demo_mode(monkeypatch):
    spawned = []
    monkeypatch.delenv('WORKSPACE_GC_IN_PROCESS', raising=False)
    _patch(monkeypatch, tenancy=True, redis=_SpyRedis(), spawned=spawned)
    sched.start(object())
    assert len(spawned) == 1


def test_every_worker_spawns_a_loop_even_when_the_lock_is_held(monkeypatch):
    """REGRESSION. start() must NOT take the lock. A lock left behind by a dead
    process or a previous container run would otherwise make every worker stand
    down at boot with nobody watching for it to expire — GC silently never runs
    again until the next restart, the exact failure this module prevents."""
    spawned = []
    monkeypatch.delenv('WORKSPACE_GC_IN_PROCESS', raising=False)
    held = _SpyRedis({sched._LOCK_KEY: 'some-dead-worker:999'})
    _patch(monkeypatch, tenancy=True, redis=held, spawned=spawned)
    sched.start(object())
    assert len(spawned) == 1, 'a held lock must not stop the loop from spawning'
    assert held.sets == [], 'start() must not touch the lock at all'


def test_starts_when_redis_is_down(monkeypatch):
    """A duplicated sweep is harmless (the GC is idempotent); no sweep is the
    failure this module exists to prevent."""
    spawned = []
    monkeypatch.delenv('WORKSPACE_GC_IN_PROCESS', raising=False)
    _patch(monkeypatch, tenancy=True, redis=None, spawned=spawned)
    sched.start(object())
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# Per-tick sweep ownership
# ---------------------------------------------------------------------------

def test_free_lock_is_claimed():
    redis = _SpyRedis()
    assert sched._owns_sweep(redis, 'host:1')
    assert redis.store[sched._LOCK_KEY] == 'host:1'


def test_a_stale_lock_from_a_dead_worker_blocks_only_until_it_expires():
    """We can't distinguish 'dead owner' from 'live owner' — the TTL does that.
    While it's held we stand down; once Redis expires it we claim it. Modelled
    by removing the key, which is what expiry does."""
    redis = _SpyRedis({sched._LOCK_KEY: 'dead-worker:999'})
    assert not sched._owns_sweep(redis, 'host:1')
    del redis.store[sched._LOCK_KEY]  # TTL lapses
    assert sched._owns_sweep(redis, 'host:1')


def test_owner_refreshes_its_own_lease():
    redis = _SpyRedis({sched._LOCK_KEY: 'host:1'})
    assert sched._owns_sweep(redis, 'host:1')
    # Refreshed without nx, so the lease only lapses if this worker dies.
    assert any(
        key == sched._LOCK_KEY and value == 'host:1' and not kw.get('nx')
        for key, value, kw in redis.sets
    )


def test_bytes_holder_is_compared_correctly():
    """redis-py can return bytes; a naive compare would make the owner think it
    lost the lock and hand the sweep to nobody."""
    redis = _SpyRedis({sched._LOCK_KEY: b'host:1'})
    assert sched._owns_sweep(redis, 'host:1')


def test_only_one_of_several_workers_sweeps_per_tick():
    redis = _SpyRedis()
    owners = [w for w in ('host:1', 'host:2', 'host:3', 'host:4')
              if sched._owns_sweep(redis, w)]
    assert owners == ['host:1']


def test_sweeps_anyway_when_redis_errors_mid_flight():
    class _Broken:
        def set(self, *a, **kw):
            raise RuntimeError('redis down')

        def get(self, key):
            raise RuntimeError('redis down')

    assert sched._owns_sweep(_Broken(), 'host:1')
    assert sched._owns_sweep(None, 'host:1')


# ---------------------------------------------------------------------------
# Nightly once-per-UTC-day marker
# ---------------------------------------------------------------------------

class _MarkerRedis:
    def __init__(self, stored=None, raise_on_get=False):
        self.stored = stored
        self.raise_on_get = raise_on_get

    def get(self, key):
        if self.raise_on_get:
            raise RuntimeError('redis down mid-flight')
        return self.stored


def test_nightly_due_when_never_run():
    assert sched._nightly_due(_MarkerRedis(None), '2026-07-28')


def test_nightly_not_due_twice_in_one_day():
    assert not sched._nightly_due(_MarkerRedis('2026-07-28'), '2026-07-28')
    # Redis clients may hand back bytes.
    assert not sched._nightly_due(_MarkerRedis(b'2026-07-28'), '2026-07-28')


def test_nightly_due_again_the_next_day():
    assert sched._nightly_due(_MarkerRedis('2026-07-27'), '2026-07-28')


def test_nightly_fails_closed_without_redis():
    """No marker means no way to stop a restart loop re-running the pass. The
    hourly sweep already covers availability, so skip rather than hammer."""
    assert not sched._nightly_due(None, '2026-07-28')
    assert not sched._nightly_due(_MarkerRedis(raise_on_get=True), '2026-07-28')
