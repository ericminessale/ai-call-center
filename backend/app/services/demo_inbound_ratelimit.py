"""
Per-caller-ID inbound rate limit for hosted-demo deployments.

Stops a single number from spamming the demo line — both as a courtesy
to the visitor pool (one bad actor shouldn't burn through every demo
agent) and as a basic abuse layer.

Implementation: Redis fixed-window counter keyed on the caller's E.164
number. Each window is one hour by default. Hitting the cap returns
``True`` from :func:`should_reject_inbound`; the SWML handler then
emits a polite "demo cap reached" message and hangs up rather than
routing to an agent.

No-ops outside DEMO_MODE — production deployments shouldn't have an
opinion on inbound call frequency at this layer (the queue itself
handles flow control there).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.services.redis_service import get_redis_client
from app.utils.demo_config import is_demo_mode

logger = logging.getLogger(__name__)


def _cap_per_window() -> int:
    """Max calls per caller-ID per window. Default 10/hr. Override via
    ``DEMO_INBOUND_CAP_PER_HOUR`` for testing or stricter limits.
    """
    raw = os.getenv('DEMO_INBOUND_CAP_PER_HOUR', '10').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 10
    return max(1, min(n, 1000))


def _window_seconds() -> int:
    """Window length in seconds. Hardcoded at 1h — the env-overridable
    knob is the cap, not the window. If you need a different window,
    change this constant.
    """
    return 3600


def _key(caller_number: str) -> str:
    return f'demo:inbound:cap:{caller_number}'


def should_reject_inbound(caller_number: Optional[str]) -> bool:
    """Increment the caller's window counter and return True if the
    inbound call should be rejected (cap exceeded).

    Falls open (returns False — accept) on missing caller number,
    Redis unavailability, or any other internal error. We'd rather
    accept a few extra calls than reject legitimate traffic during a
    transient backend hiccup.
    """
    if not is_demo_mode() or not caller_number:
        return False

    redis_client = get_redis_client()
    if redis_client is None:
        logger.warning("demo_inbound_ratelimit: Redis unavailable — accepting call")
        return False

    cap = _cap_per_window()
    key = _key(caller_number)

    try:
        # INCR is atomic. If this is the first call in a window the
        # subsequent EXPIRE seeds the TTL; on every later call within
        # the window the EXPIRE is a no-op refresh (acceptable — the
        # window slides slightly, but at most by one increment).
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, _window_seconds())
    except Exception as e:
        logger.warning("demo_inbound_ratelimit: Redis error %s — accepting call", e)
        return False

    if count > cap:
        logger.info(
            "demo_inbound_ratelimit: caller %s rejected (count=%d, cap=%d)",
            caller_number, count, cap,
        )
        return True
    return False


def reject_swml() -> dict:
    """Return the SWML payload sent to a rate-limited caller.

    Polite, brief, hangs up cleanly. Doesn't expose the cap value
    (don't tell abusers "9 more and you're cut off").
    """
    return {
        'version': '1.0.0',
        'sections': {
            'main': [
                'answer',
                {
                    'play': {
                        'urls': [
                            'say:You have reached the per-day demo limit '
                            'from this phone number. Please try again later.'
                        ]
                    }
                },
                'hangup',
            ]
        },
    }
