"""URL utilities for handling external URLs and proxies."""
import os
from urllib.parse import quote, urlparse, urlunparse

from flask import request

# External URL for SignalWire callbacks (e.g., ngrok URL)
# Set this in .env when developing locally so SignalWire can reach your server
EXTERNAL_URL = os.getenv('EXTERNAL_URL')


def get_base_url():
    """Get the base URL for callbacks, handling proxy headers.

    Priority:
    1. EXTERNAL_URL environment variable (for local dev with ngrok)
    2. X-Forwarded-Host header (when behind ngrok/proxy)
    3. request.host_url (fallback)

    Usage:
        Add EXTERNAL_URL=https://your-ngrok-url.ngrok.io to your .env file
        when developing locally with ngrok.
    """
    # If EXTERNAL_URL is set, always use it
    if EXTERNAL_URL:
        return EXTERNAL_URL.rstrip('/')

    forwarded_host = request.headers.get('X-Forwarded-Host')
    forwarded_proto = request.headers.get('X-Forwarded-Proto', 'https')

    if forwarded_host:
        if 'ngrok' in forwarded_host:
            forwarded_proto = 'https'
        return f"{forwarded_proto}://{forwarded_host}"
    else:
        return request.host_url.rstrip('/')


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
