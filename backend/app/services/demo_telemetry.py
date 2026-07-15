"""Lightweight ops counters for the hosted demo (Phase 5 telemetry).

Daily Redis counters under ``demo:stats:{name}:{YYYYMMDD}``. The DB can't
answer "workspaces created per day" — reaping deletes the rows — so the
event sites bump a counter instead. 35-day TTL keeps the keyspace bounded
without a cleanup job; the operator view reads the last 7 days.

Counter names in use:
    ws_created       — provision_workspace (fresh workspace, not resume)
    ws_reaped        — reap_workspace
    ws_verified      — pair_number successful fresh pairing
    inbound_rejected — verify-first rejects (also the legacy total counter
                       ``demo:inbound:rejected``)

Everything here is best-effort: counters must never break the paths that
bump them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)

_TTL_SECONDS = 35 * 24 * 3600


def _day_key(name: str, day: datetime) -> str:
    return f"demo:stats:{name}:{day.strftime('%Y%m%d')}"


def bump_daily(name: str) -> None:
    """Increment today's counter for ``name``. Best-effort."""
    try:
        redis_client = get_redis_client()
        if redis_client is None:
            return
        key = _day_key(name, datetime.utcnow())
        if redis_client.incr(key) == 1:
            redis_client.expire(key, _TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("demo_telemetry: bump %s failed: %s", name, exc)


def read_daily_series(name: str, days: int = 7) -> list[dict]:
    """Last ``days`` days of a counter, oldest first: [{date, count}]."""
    out: list[dict] = []
    try:
        redis_client = get_redis_client()
        now = datetime.utcnow()
        for offset in range(days - 1, -1, -1):
            day = now - timedelta(days=offset)
            count = 0
            if redis_client is not None:
                raw = redis_client.get(_day_key(name, day))
                if raw is not None:
                    try:
                        count = int(raw)
                    except (TypeError, ValueError):
                        count = 0
            out.append({'date': day.strftime('%Y-%m-%d'), 'count': count})
    except Exception as exc:  # noqa: BLE001
        logger.debug("demo_telemetry: series %s failed: %s", name, exc)
    return out
