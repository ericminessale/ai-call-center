"""URL utilities for handling external URLs and proxies."""
import os
from urllib.parse import quote, urlparse, urlunparse

from flask import request

# External URL for SignalWire callbacks (e.g., ngrok URL)
# Set this in .env when developing locally so SignalWire can reach your server
EXTERNAL_URL = os.getenv('EXTERNAL_URL')


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
