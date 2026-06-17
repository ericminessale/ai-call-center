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
    WEBHOOK_AUTH_REQUIRED=false  # OPTIONAL escape hatch. Default (unset)
                                 # ENFORCES; only 'false' downgrades to
                                 # soft-logging.

Producers (the AI agents and any backend code that hands SignalWire a URL
to call back) must inject these credentials into the URL — see
``app.utils.url_utils.signed_webhook_url`` for the helper.

Secure-by-default: :func:`require_webhook_auth` ENFORCES (rejects with 401)
unless ``WEBHOOK_AUTH_REQUIRED=false`` is explicitly set. Soft mode logs a
warning when credentials are missing or wrong but still calls the handler;
it exists only as a temporary migration window for a deployment that hasn't
yet rotated its webhook URLs to carry credentials. The private
backend⇄ai-agents routes use :func:`require_internal_auth`, which ignores
this flag and ALWAYS enforces (they expose decrypted credentials and a
destructive reset). The app also fail-fasts at boot when the credentials are
unset, unless soft mode was explicitly chosen — see ``app/__init__.py``.
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
    """Whether failed webhook auth REJECTS (vs soft-logs).

    Secure-by-default: enforcement is ON unless ``WEBHOOK_AUTH_REQUIRED`` is
    explicitly set to 'false'. The 'false' escape hatch exists only as a
    short migration window (webhook URLs not yet rotated to carry creds) and
    logs loudly while active.
    """
    return os.getenv('WEBHOOK_AUTH_REQUIRED', 'true').strip().lower() != 'false'


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


def _validate_request_auth() -> tuple[bool, bool]:
    """Validate the inbound HTTP Basic header against configured creds.

    Returns ``(configured, authorized)``:
      - ``configured`` is False when WEBHOOK_AUTH_USER/PASSWORD aren't set.
      - ``authorized`` is True only when the header matches (constant-time).
    """
    expected_user, expected_pw = _expected_credentials()
    if not expected_user or not expected_pw:
        return False, False
    provided = _parse_basic_auth(request.headers.get('Authorization', ''))
    authorized = provided is not None and (
        hmac.compare_digest(provided[0], expected_user)
        and hmac.compare_digest(provided[1], expected_pw)
    )
    return True, authorized


def require_webhook_auth(f: Callable) -> Callable:
    """Validate HTTP Basic Auth on inbound SignalWire webhook requests.

    Secure-by-default (see :func:`_enforce_mode`): rejects with 401 on a
    missing/wrong header unless ``WEBHOOK_AUTH_REQUIRED=false`` downgrades it
    to soft-logging. If credentials aren't configured at all, enforce mode
    returns 500 (fail loud) — though the app also fail-fasts at boot in that
    case unless soft mode was explicitly chosen.
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        enforce = _enforce_mode()
        configured, authorized = _validate_request_auth()

        if not configured:
            if enforce:
                logger.error(
                    "%s: webhook auth enforced but WEBHOOK_AUTH_USER/PASSWORD "
                    "are not configured — refusing request",
                    request.path,
                )
                return jsonify({'error': 'Webhook auth not configured'}), 500
            # Soft mode: nothing to compare against.
            return f(*args, **kwargs)

        if not authorized:
            if enforce:
                logger.warning("%s: webhook auth failed (rejected)", request.path)
                return (
                    jsonify({'error': 'Unauthorized webhook'}),
                    401,
                    {'WWW-Authenticate': 'Basic realm="webhook"'},
                )
            logger.warning(
                "%s: webhook auth failed (allowed — soft mode; unset "
                "WEBHOOK_AUTH_REQUIRED or remove the 'false' override to enforce)",
                request.path,
            )

        return f(*args, **kwargs)

    return wrapper


def require_internal_auth(f: Callable) -> Callable:
    """Strict HTTP Basic auth for the private backend⇄ai-agents API.

    Unlike :func:`require_webhook_auth`, this NEVER runs in soft mode. The
    internal routes expose decrypted MCP-gateway credentials and a
    destructive demo reset, so they reject unauthenticated callers
    regardless of ``WEBHOOK_AUTH_REQUIRED``. Returns 500 if credentials are
    unconfigured (fail loud rather than silently open).
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        configured, authorized = _validate_request_auth()
        if not configured:
            logger.error(
                "%s: internal auth required but WEBHOOK_AUTH_USER/PASSWORD "
                "are not configured — refusing request",
                request.path,
            )
            return jsonify({'error': 'Internal auth not configured'}), 500
        if not authorized:
            logger.warning("%s: internal auth failed (rejected)", request.path)
            return (
                jsonify({'error': 'Unauthorized'}),
                401,
                {'WWW-Authenticate': 'Basic realm="internal"'},
            )
        return f(*args, **kwargs)

    return wrapper
