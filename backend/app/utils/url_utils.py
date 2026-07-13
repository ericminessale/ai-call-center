"""URL utilities for handling external URLs and proxies."""
import hashlib
import hmac
import os
from urllib.parse import quote, urlparse, urlunparse

from flask import request

# External URL for SignalWire callbacks (e.g., ngrok URL)
# Set this in .env when developing locally so SignalWire can reach your server
EXTERNAL_URL = os.getenv('EXTERNAL_URL')


def call_context_token(call_db_id) -> str:
    """Unforgeable per-call token binding a ``call_db_id`` to backend intent.

    Phase 4 (§7.1 hardening): the AI agent routes are PUBLIC and resolve
    their tenant config from ``?call_db_id=`` via /api/internal/call-context.
    call_db_id is a sequential DB id, so without a signature an
    unauthenticated attacker could hit ``/receptionist?call_db_id=N`` and
    have the agent render another workspace's queue/branding config
    (confused deputy). The BACKEND mints this token when it hands an agent
    URL to SignalWire; call-context rejects any call_db_id whose token
    doesn't verify. The agent only ever FORWARDS the token it received — it
    never mints one — so a caller who can't produce a valid token for a
    call_db_id gets the inert template config instead of a tenant's.

    HMAC over the shared WEBHOOK_AUTH secret (already the backend↔agent
    trust anchor). Truncated to 32 hex chars — ample against online
    guessing given call-context also sits behind internal Basic auth.
    """
    key = (os.getenv('WEBHOOK_AUTH_PASSWORD')
           or os.getenv('JWT_SECRET_KEY') or '').encode()
    msg = f'call-context:{call_db_id}'.encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_call_context_token(call_db_id, token) -> bool:
    """Constant-time check of a :func:`call_context_token`. False on any
    missing/blank/mismatched token (fail closed)."""
    if not token:
        return False
    return hmac.compare_digest(call_context_token(call_db_id), str(token))


def get_base_url():
    """Get the base URL for callbacks (the host SignalWire calls back into).

    SEC-05 fix (2026-06-02 audit): callbacks formerly fell back to the
    request's ``X-Forwarded-Host`` header (and ultimately ``request.host_url``)
    when ``EXTERNAL_URL`` was unset. Both headers are attacker-controllable
    on an inbound request — an attacker who can reach a webhook endpoint
    (now auth-gated, but the fail-fast here is defense in depth) can
    poison the callback URLs we hand SignalWire, forking subsequent calls
    into their domain (where they receive payload data, can drop the call,
    etc.). ``EXTERNAL_URL`` is the only trustworthy source — operator-
    controlled, not request-derived.

    Operators MUST set ``EXTERNAL_URL`` in .env to the public origin
    SignalWire calls back into (e.g. the ngrok URL during local dev, the
    real domain in production). Failing to set it now raises at the call
    site rather than silently degrading.
    """
    if EXTERNAL_URL:
        return EXTERNAL_URL.rstrip('/')

    raise RuntimeError(
        "EXTERNAL_URL is not set. Set it in .env to the public origin "
        "SignalWire calls back into (ngrok URL in dev, real domain in "
        "prod). The previous X-Forwarded-Host fallback was a callback-"
        "hijacking vector — see SEC-05 in REMEDIATION_2026-06-02.md."
    )


def signed_webhook_url(url: str) -> str:
    """Embed WEBHOOK_AUTH credentials into a webhook URL.

    SignalWire's standard inbound-webhook auth scheme is HTTP Basic, with
    credentials parsed out of the ``user:pass@host`` portion of the URL
    handed back to the platform. Producers (AI agents, backend code) call
    this helper when constructing a URL that SignalWire will later POST to.

    If ``WEBHOOK_AUTH_USER`` / ``WEBHOOK_AUTH_PASSWORD`` aren't set, the URL
    is returned unchanged — pairs with the soft-mode behavior of
    :func:`app.utils.webhook_auth.require_webhook_auth`.
    """
    user = os.getenv('WEBHOOK_AUTH_USER')
    pw = os.getenv('WEBHOOK_AUTH_PASSWORD')
    if not user or not pw:
        return url
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params,
                       parsed.query, parsed.fragment))
