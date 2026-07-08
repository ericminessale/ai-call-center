"""
Per-client-IP rate limiting for public unauthenticated endpoints (SEC-06).

The hosted demo exposes two credential-adjacent endpoints to the open
internet: ``POST /api/auth/login`` (password guessing) and
``POST /api/demo/start`` (persona-pool draining). Both get a Redis
fixed-window counter keyed on client IP.

Same implementation shape as ``services/demo_inbound_ratelimit`` (INCR +
EXPIRE fixed window) and the same failure posture: fail open on Redis
trouble — a transient backend hiccup should never lock out legitimate
logins.

Client IP comes from ``request.remote_addr``. Behind a reverse proxy that
is only correct when ``TRUSTED_PROXY_COUNT`` is set (see ProxyFix wiring
in ``create_app``) — otherwise every client appears as the proxy's IP and
the limit becomes a crude global cap, which is still better than nothing.

Unlike the demo-only gates this is NOT gated on DEMO_MODE — login
rate limiting is just as valuable on a clone-and-own deployment.
"""

from __future__ import annotations

import logging
from functools import wraps

from flask import jsonify, request

logger = logging.getLogger(__name__)

RATE_LIMITED_RESPONSE = {
    'error': 'Too many requests. Please wait a moment and try again.',
    'code': 'rate_limited',
}


def _client_ip() -> str:
    return request.remote_addr or 'unknown'


def rate_limit(scope: str, limit: int, window_seconds: int):
    """Decorator: allow at most ``limit`` requests per ``window_seconds``
    per client IP for this endpoint. Over the cap returns 429.

    ``scope`` namespaces the Redis key so endpoints don't share budgets.
    """

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from app.services.redis_service import get_redis_client

            redis_client = get_redis_client()
            if redis_client is None:
                logger.warning("rate_limit[%s]: Redis unavailable — allowing", scope)
                return f(*args, **kwargs)

            key = f'ratelimit:{scope}:{_client_ip()}'
            try:
                # INCR+TTL in one round trip. Setting the TTL whenever it
                # reads -1 (not just on count==1) self-heals the stuck-key
                # case where a prior EXPIRE failed after its INCR — without
                # this the key never expires and the IP 429s forever.
                pipe = redis_client.pipeline()
                pipe.incr(key)
                pipe.ttl(key)
                count, ttl = pipe.execute()
                if ttl == -1:
                    redis_client.expire(key, window_seconds)
            except Exception as e:
                logger.warning("rate_limit[%s]: Redis error %s — allowing", scope, e)
                return f(*args, **kwargs)

            if count > limit:
                logger.info(
                    "rate_limit[%s]: %s over cap (count=%d, limit=%d/%ds)",
                    scope, _client_ip(), count, limit, window_seconds,
                )
                return jsonify(RATE_LIMITED_RESPONSE), 429

            return f(*args, **kwargs)

        return wrapped

    return decorator
