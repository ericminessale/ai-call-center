"""In-process scheduler for the hosted-demo workspace GC (HIGH-7).

The two GC entry points in :mod:`app.services.demo_reset` were reachable only
over HTTP, driven by the ``demo-reset`` cron container — which is defined
**only** in the operator's gitignored ``docker-compose.demo.yml``. If that
overlay isn't running (or is lost), nothing ever calls them: a workspace's
``status`` stays ACTIVE until a reaper deletes it, so expired workspaces
accumulate until ``MAX_WORKSPACES`` is reached and every new visitor gets a
permanent 503. This module removes that single point of failure by running the
same functions on a timer inside the app.

Inert outside hosted-demo mode. A clone-and-own deployment has exactly one
workspace that must never be GC'd, so the loop is never even started there —
and ``reap_expired_workspaces``/``nightly_safety_pass`` refuse independently, so
this is belt-and-braces rather than the only guard.

Safe alongside the cron container if the operator keeps it: both paths call the
same idempotent functions, which only touch already-expired workspaces. A Redis
lock keeps exactly one gunicorn worker sweeping, and the nightly pass is marked
done-for-the-UTC-day in Redis so a worker restart can't re-run it repeatedly.

Ownership is re-elected **every tick**, deliberately unlike ``call_watchdog``,
which acquires its lock once at ``start()`` and gives up for the process
lifetime if it loses. That pattern has a hole: a lock written by a previous
container run outlives the process that held it (Redis persists; the TTL can't
know the owner died), so on a quick restart EVERY worker sees the lock held,
stands down permanently, and nobody is left watching for it to expire — GC then
silently never runs until the next restart, which is precisely the failure this
module exists to eliminate. Here every worker runs a loop, and each tick asks
whether it owns the sweep; the loser is cheap (one Redis call per interval) and
the fleet self-heals within one interval of an owner dying.

Env:
  ``WORKSPACE_GC_IN_PROCESS``      'false' disables this scheduler entirely
                                   (use if you'd rather drive GC externally).
  ``WORKSPACE_GC_INTERVAL_SECONDS`` tick/hourly-GC period, default 3600.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime

logger = logging.getLogger(__name__)

# Cross-worker sweep-ownership lock. Holds the owner's identity (not just '1')
# so a holder can tell "still mine" from "someone else's" and refresh its own.
_LOCK_KEY = 'workspace_gc_scheduler_owner'


def _worker_id() -> str:
    """Identity written into the ownership lock. Host-qualified so this stays
    correct if the demo is ever scaled to more than one backend host."""
    return f'{socket.gethostname()}:{os.getpid()}'
# Records the UTC date the nightly pass last completed, so restarts and worker
# handovers can't re-run it. Two days of TTL so the marker outlives one cycle.
_NIGHTLY_MARKER_KEY = 'workspace_gc_last_nightly_utc_date'
_NIGHTLY_MARKER_TTL = 2 * 24 * 60 * 60
# Short delay before the first sweep so boot isn't competing with migrations,
# the seat-pool seed and the fabric sync for the DB/API.
_INITIAL_DELAY_SECONDS = 60


def in_process_gc_enabled() -> bool:
    """True unless the operator opted out. Default-on: the failure mode this
    exists to prevent (silent 503 for every visitor) is worse than a redundant
    sweep for anyone still running the cron container."""
    return os.getenv('WORKSPACE_GC_IN_PROCESS', '').strip().lower() != 'false'


def gc_interval_seconds() -> int:
    """Tick period. ``WORKSPACE_GC_INTERVAL_SECONDS``, default 3600 (matching
    the cron container's hourly cadence), clamped to [60, 86400]."""
    raw = os.getenv('WORKSPACE_GC_INTERVAL_SECONDS', '3600').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3600
    return max(60, min(n, 24 * 60 * 60))


def _lock_ttl() -> int:
    return max(120, gc_interval_seconds() * 3)


def _run_hourly(app) -> None:
    """One hourly-GC sweep. Mirrors POST /api/internal/workspace-gc."""
    from app.services.demo_reset import reap_expired_workspaces

    with app.app_context():
        summary = reap_expired_workspaces()
    # Only shout when something was actually collected — an idle demo would
    # otherwise write a log line every hour forever.
    if summary.get('reaped'):
        logger.warning('[workspace_gc] hourly: %s', summary)
    else:
        logger.info('[workspace_gc] hourly: nothing to reap')


def _nightly_due(redis_client, today: str) -> bool:
    """Has the nightly pass already run for this UTC day?

    Fails CLOSED (returns False) when Redis is unavailable: without the marker
    there's no way to avoid re-running the pass on every restart, and the
    hourly sweep already covers the part that matters for availability. The
    nightly extras — MAX_WORKSPACES cap enforcement and template hygiene — can
    wait for Redis to come back.
    """
    if redis_client is None:
        return False
    try:
        return redis_client.get(_NIGHTLY_MARKER_KEY) not in (today, today.encode())
    except Exception as exc:
        logger.warning('[workspace_gc] nightly marker read failed (%s) — skipping', exc)
        return False


def _maybe_run_nightly(app, redis_client) -> None:
    """Run the nightly safety pass at most once per UTC day.

    Deliberately not pinned to 00:00 UTC like the cron container: what matters
    is that the cap enforcement and template cleanup happen daily, not the exact
    hour, and a wall-clock target would need a sleep-until that a restart
    silently skips.
    """
    from app.services.demo_reset import nightly_safety_pass

    today = datetime.utcnow().strftime('%Y-%m-%d')
    if not _nightly_due(redis_client, today):
        return

    with app.app_context():
        summary = nightly_safety_pass()
    logger.warning('[workspace_gc] nightly safety pass: %s', summary)
    try:
        redis_client.set(_NIGHTLY_MARKER_KEY, today, ex=_NIGHTLY_MARKER_TTL)
    except Exception as exc:
        # Worst case the pass repeats on the next tick. Idempotent, just noisy.
        logger.warning('[workspace_gc] nightly marker write failed: %s', exc)


def _owns_sweep(redis_client, me: str) -> bool:
    """Does this worker own this tick's sweep?

    Take the lock if it's free; refresh it if it's already ours; stand down for
    this tick if another live worker holds it. Re-asked every tick so a lock
    left behind by a dead process (or a previous container run) is picked up
    within one TTL instead of parking the whole fleet forever.

    No Redis → return True: a duplicated sweep is harmless (the GC only touches
    already-expired workspaces and is idempotent); no sweep at all isn't.
    """
    if redis_client is None:
        return True
    try:
        if redis_client.set(_LOCK_KEY, me, nx=True, ex=_lock_ttl()):
            logger.warning('[workspace_gc] this worker (%s) now owns the sweep', me)
            return True
        holder = redis_client.get(_LOCK_KEY)
        if isinstance(holder, bytes):
            holder = holder.decode('utf-8', 'replace')
        if holder == me:
            # Still ours — extend the lease so it only lapses if we actually die.
            redis_client.set(_LOCK_KEY, me, ex=_lock_ttl())
            return True
        return False
    except Exception as exc:
        logger.warning('[workspace_gc] ownership check failed (%s) — sweeping anyway', exc)
        return True


def _run_loop(app) -> None:
    """Background loop. Runs forever; logs and continues on any error."""
    from app import socketio
    from app.services.redis_service import get_redis_client

    interval = gc_interval_seconds()
    me = _worker_id()
    logger.info('[workspace_gc] loop live in %s (interval=%ds)', me, interval)
    socketio.sleep(_INITIAL_DELAY_SECONDS)

    while True:
        try:
            redis_client = None
            try:
                redis_client = get_redis_client()
            except Exception:
                pass

            if _owns_sweep(redis_client, me):
                _run_hourly(app)
                _maybe_run_nightly(app, redis_client)
        except Exception as exc:
            # Never let a transient failure kill the loop — a dead reaper is
            # exactly the MAX_WORKSPACES trap this module exists to prevent.
            logger.error('[workspace_gc] sweep error: %s', exc, exc_info=True)
        socketio.sleep(interval)


def start(app) -> None:
    """Spawn the GC loop as a Socket.IO background task in this worker.

    No-ops when hosted-demo mode is off or the operator disabled it. Every
    worker gets a loop; :func:`_owns_sweep` decides which one actually sweeps on
    any given tick, so there's deliberately no boot-time lock here — see the
    module docstring for why that pattern is unsafe for this task.
    """
    from app import socketio
    from app.utils.demo_config import tenancy_mode_active

    if not tenancy_mode_active():
        # Clone-and-own: nothing to collect, and demo_reset would refuse anyway.
        return
    if not in_process_gc_enabled():
        logger.warning('[workspace_gc] disabled via WORKSPACE_GC_IN_PROCESS=false')
        return

    socketio.start_background_task(_run_loop, app)
