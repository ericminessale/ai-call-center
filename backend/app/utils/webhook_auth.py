"""
Webhook authentication for SignalWire-originated requests.

SignalWire's standard mechanism for authenticating inbound webhooks (SWML
script callbacks, transcription/summary callbacks, etc.) is HTTP Basic
Auth credentials embedded in the request URL. The platform passes those
credentials through as a standard ``Authorization: Basic ...`` header on
every callback. This module validates that header.

Configuration via env (in ``.env``):

    WEBHOOK_AUTH_USER=<username>
    WEBHOOK_AUTH_PASSWORD=<random-string>
    WEBHOOK_AUTH_REQUIRED=true   # set to 'true' to enforce; missing/false = log-only

Producers (the AI agents and any backend code that hands SignalWire a URL
to call back) must inject these credentials into the URL — see
``app.utils.url_utils.signed_webhook_url`` for the helper.

If ``WEBHOOK_AUTH_REQUIRED`` is not 'true', this decorator is a soft check:
it logs a warning when credentials are missing or wrong but still calls
the handler. That mode exists so adding the decorator broadly doesn't
break a running deployment that hasn't yet rotated webhook URLs.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
from functools import wraps
from typing import Callable

from flask import jsonify, request

logger = logging.getLogger(__name__)


def _expected_credentials() -> tuple[str | None, str | None]:
    return os.getenv('WEBHOOK_AUTH_USER'), os.getenv('WEBHOOK_AUTH_PASSWORD')


def _enforce_mode() -> bool:
    return os.getenv('WEBHOOK_AUTH_REQUIRED', '').lower() == 'true'


def _parse_basic_auth(header_value: str) -> tuple[str, str] | None:
    """Decode a 'Basic <b64>' header into (user, password). Returns None on
    any parse error rather than raising — caller treats that as auth failed."""
    if not header_value or not header_value.lower().startswith('basic '):
        return None
    try:
        decoded = base64.b64decode(header_value.split(' ', 1)[1]).decode('utf-8')
    except Exception:
        return None
    if ':' not in decoded:
        return None
    user, _, password = decoded.partition(':')
    return user, password


def require_webhook_auth(f: Callable) -> Callable:
    """Validate HTTP Basic Auth on inbound webhook requests.

    Behavior depends on env:
      - ``WEBHOOK_AUTH_USER`` + ``WEBHOOK_AUTH_PASSWORD`` set + ``WEBHOOK_AUTH_REQUIRED=true``:
            enforce; reject with 401 if header missing/wrong.
      - credentials set but ``WEBHOOK_AUTH_REQUIRED`` not 'true':
            soft-check; log a warning on mismatch but call the handler.
      - credentials not set:
            log once-per-startup warning, call the handler. Production should
            never run in this state.
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        expected_user, expected_pw = _expected_credentials()
        enforce = _enforce_mode()

        if not expected_user or not expected_pw:
            if enforce:
                logger.error(
                    "%s: WEBHOOK_AUTH_REQUIRED=true but credentials are "
                    "not configured — refusing request",
                    request.path,
                )
                return jsonify({'error': 'Webhook auth not configured'}), 500
            # Soft mode: nothing to compare against.
            return f(*args, **kwargs)

        provided = _parse_basic_auth(request.headers.get('Authorization', ''))
        ok = provided is not None and (
            hmac.compare_digest(provided[0], expected_user)
            and hmac.compare_digest(provided[1], expected_pw)
        )

        if not ok:
            if enforce:
                logger.warning("%s: webhook auth failed (rejected)", request.path)
                return (
                    jsonify({'error': 'Unauthorized webhook'}),
                    401,
                    {'WWW-Authenticate': 'Basic realm="webhook"'},
                )
            logger.warning(
                "%s: webhook auth failed (allowed — set "
                "WEBHOOK_AUTH_REQUIRED=true to enforce)",
                request.path,
            )

        return f(*args, **kwargs)

    return wrapper
