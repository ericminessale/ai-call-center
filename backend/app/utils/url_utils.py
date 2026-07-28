"""URL utilities for handling external URLs and proxies."""
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote, urlparse, urlunparse

from flask import request

# External URL for SignalWire callbacks (e.g., ngrok URL)
# Set this in .env when developing locally so SignalWire can reach your server
EXTERNAL_URL = os.getenv('EXTERNAL_URL')

logger = logging.getLogger(__name__)

# A monitor tap can legitimately remain active for a long call, but the URL
# should not be a permanent bearer credential. This matches the two-hour
# Redis lifetime used for tap control IDs in call_control.py.
TAP_STREAM_URL_TTL_SECONDS = 2 * 60 * 60


def _internal_signing_secret() -> str | None:
    """Secret for backend-minted HMAC tokens (ctk, tap-stream).

    Keyed on the segregated INTERNAL_AUTH secret so a leak of the
    SignalWire-facing WEBHOOK_AUTH creds — which travel semi-publicly in the
    ``user:pass@host`` of SWML callback URLs — can't forge these tokens. Falls
    back to WEBHOOK_AUTH_PASSWORD, then JWT_SECRET_KEY, so an unconfigured
    deployment behaves exactly as before. Mirrors
    ``webhook_auth._expected_internal_credentials``.
    """
    return (os.getenv('INTERNAL_AUTH_PASSWORD')
            or os.getenv('WEBHOOK_AUTH_PASSWORD')
            or os.getenv('JWT_SECRET_KEY'))


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

    HMAC over the segregated INTERNAL_AUTH secret (see
    :func:`_internal_signing_secret`) — NOT the semi-public WEBHOOK_AUTH creds
    that ride in rendered SWML. Truncated to 32 hex chars — ample against
    online guessing given call-context also sits behind internal Basic auth.
    """
    key = (_internal_signing_secret() or '').encode()
    msg = f'call-context:{call_db_id}'.encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_call_context_token(call_db_id, token) -> bool:
    """Constant-time check of a :func:`call_context_token`. False on any
    missing/blank/mismatched token (fail closed)."""
    if not token:
        return False
    return hmac.compare_digest(call_context_token(call_db_id), str(token))


def _tap_stream_signing_key() -> bytes:
    """Return the server-only key used for tap-ingest URLs.

    Keyed on the segregated INTERNAL_AUTH secret (see
    :func:`_internal_signing_secret`), falling back to WEBHOOK_AUTH_PASSWORD
    then JWT_SECRET_KEY — never an empty/public key. The tap URL grants live
    call-audio access, so its signing key must not be the semi-public
    WEBHOOK_AUTH cred embedded in rendered SWML.
    """
    secret = _internal_signing_secret()
    if not secret:
        raise RuntimeError(
            'Tap stream signing requires INTERNAL_AUTH_PASSWORD, '
            'WEBHOOK_AUTH_PASSWORD or JWT_SECRET_KEY'
        )
    return secret.encode()


def tap_stream_signature(call_id: str, expires_at: int) -> str:
    """Sign one SignalWire tap-ingest URL for a specific call and expiry."""
    message = f'tap-stream:{call_id}:{int(expires_at)}'.encode()
    return hmac.new(
        _tap_stream_signing_key(), message, hashlib.sha256,
    ).hexdigest()


def verify_tap_stream_signature(
    call_id: str,
    expires_at,
    signature,
    *,
    now: int | None = None,
) -> bool:
    """Validate a call-bound tap URL in constant time and fail closed."""
    if not signature:
        return False
    try:
        expiry = int(expires_at)
    except (TypeError, ValueError):
        return False

    current = int(time.time()) if now is None else int(now)
    if expiry < current:
        return False
    # Reject unexpectedly long-lived URLs even if a future call site signs
    # one by mistake. Sixty seconds allows harmless clock/rounding skew.
    if expiry > current + TAP_STREAM_URL_TTL_SECONDS + 60:
        return False
    try:
        expected = tap_stream_signature(str(call_id), expiry)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, str(signature))


def signed_tap_stream_url(
    base_ws_url: str,
    call_id: str,
    *,
    now: int | None = None,
) -> str:
    """Build the short-lived WebSocket URL handed to SignalWire calling.tap."""
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + TAP_STREAM_URL_TTL_SECONDS
    call_path = quote(str(call_id), safe='')
    signature = tap_stream_signature(str(call_id), expires_at)
    return (
        f"{base_ws_url.rstrip('/')}/ws/tap-stream/{call_path}"
        f"?expires={expires_at}&signature={signature}"
    )


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


# ---------------------------------------------------------------------------
# Webhook callback URL authentication (CRITICAL-1 Phase 2 / HIGH-4)
# ---------------------------------------------------------------------------
# SignalWire's native inbound-webhook auth is HTTP Basic parsed out of the
# ``user:pass@host`` we hand back to the platform. That makes WEBHOOK_AUTH a
# permanent, global bearer credential printed into every rendered SWML — and
# the agent routes that render it are public, so it leaks. Phase 1 stopped the
# leak from unlocking /api/internal/* by segregating INTERNAL_AUTH. Phase 2 is
# this: stop putting a credential in the URL at all.
#
# A callback URL instead carries an HMAC token bound to its own PATH, signed
# with the segregated internal secret (never rendered anywhere). Leaking one
# grants at most "POST this one endpoint", not "hold the install's webhook
# password".
#
# Rollout is flag-switched and BOTH schemes are accepted inbound, so producers
# can move without a flag-day: see WEBHOOK_URL_AUTH below and
# ``webhook_auth.require_webhook_auth``.
WEBHOOK_TOKEN_PARAM = '_wt'
WEBHOOK_TOKEN_EXPIRY_PARAM = '_wexp'

# Default lifetime for a per-call callback token. Has to outlive the whole
# call: /api/webhooks/call-status fires at hangup and transcription fires
# throughout, and call_watchdog tolerates an 'active' call for 4h. 12h leaves
# room without making the token long-lived enough to be worth harvesting.
WEBHOOK_URL_TOKEN_TTL_SECONDS = 12 * 60 * 60


def webhook_url_auth_mode() -> str:
    """``'basic'`` (default) or ``'token'``.

    Defaults to 'basic' — today's behaviour — on purpose. Flipping the scheme
    changes what every SignalWire callback carries, and a mistake means silent
    callback failure (no status updates, no transcripts), which only a live
    PSTN call reveals. The inbound side accepts both regardless, so an operator
    flips this, places one test call, and flips back if anything is off.
    """
    return 'token' if os.getenv(
        'WEBHOOK_URL_AUTH', '').strip().lower() == 'token' else 'basic'


def _webhook_token_path(url: str) -> str:
    """The path a token is bound to. Path only — host and query are excluded so
    an ngrok rotation or an extra query param doesn't invalidate a live token."""
    return urlparse(url).path or '/'


def _webhook_token_signing_secret() -> str | None:
    """The ONLY key allowed to sign/verify a Phase 2 callback token.

    Deliberately NOT :func:`_internal_signing_secret`, which falls back to
    ``WEBHOOK_AUTH_PASSWORD``. Signing with that would be self-defeating: it's
    the credential Phase 2 exists to stop exposing, so a leaked SWML would let
    anyone forge a token for any path — the scheme would look hardened while
    providing nothing. No fallback, no ``JWT_SECRET_KEY`` either: token mode
    requires a real segregated secret or it doesn't engage at all.

    Both minting and verification go through here, so the two can never
    disagree about which key is authoritative — a mint-strict / verify-loose
    split would accept forged tokens.
    """
    return os.getenv('INTERNAL_AUTH_PASSWORD') or None


def webhook_url_token(url: str, expires_at: int | None) -> str:
    """HMAC binding one callback URL's PATH (and optional expiry) to us.

    Signed with :func:`_internal_signing_secret` — the segregated INTERNAL_AUTH
    secret — NOT WEBHOOK_AUTH. Signing with the credential we're trying to stop
    exposing would defeat the point.

    ``expires_at=None`` mints a non-expiring token. That is required for URLs
    stored persistently on the SignalWire side (a phone number's
    ``call_relay_script_url``, the managed swml_webhook resources): those are
    written once and fetched for months, so a TTL there would silently break
    inbound calls when it lapsed. Still strictly better than the status quo —
    path-scoped, and not a credential that unlocks anything else.
    """
    key = (_webhook_token_signing_secret() or '').encode()
    expiry_part = '' if expires_at is None else str(int(expires_at))
    msg = f'webhook-url:{_webhook_token_path(url)}:{expiry_part}'.encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_webhook_url_token(
    path: str,
    token,
    expires_at=None,
    *,
    now: int | None = None,
) -> bool:
    """Constant-time check of a callback token. Fails closed on anything odd.

    ``expires_at`` absent/blank means the caller presented a non-expiring
    token; it still has to match the non-expiring signature for this path, so
    an attacker can't downgrade an expiring token by dropping the parameter.
    """
    if not token:
        return False
    if not _webhook_token_signing_secret():
        return False

    expiry: int | None = None
    if expires_at not in (None, ''):
        try:
            expiry = int(expires_at)
        except (TypeError, ValueError):
            return False
        current = int(time.time()) if now is None else int(now)
        if expiry < current:
            return False

    # Sign against the path alone — see _webhook_token_path.
    expected = webhook_url_token(f'http://placeholder{path}', expiry)
    return hmac.compare_digest(expected, str(token))


def _basic_auth_webhook_url(url: str) -> str:
    """Today's scheme: WEBHOOK_AUTH creds in the ``user:pass@host``."""
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


def _token_webhook_url(url: str, *, ttl_seconds: int | None) -> str:
    """Phase 2 scheme: a path-bound HMAC in the query string, no credentials."""
    if not _webhook_token_signing_secret():
        # No segregated secret to sign with. Degrade to the Basic scheme
        # rather than either (a) handing SignalWire a URL our own inbound
        # check would reject, or (b) signing with WEBHOOK_AUTH_PASSWORD —
        # which would look hardened while being forgeable from any leaked
        # SWML. Loud, because the operator asked for token mode and isn't
        # getting it.
        logger.warning(
            'WEBHOOK_URL_AUTH=token ignored: INTERNAL_AUTH_PASSWORD is not '
            'set, so there is no secret that is safe to sign callback '
            'tokens with. Falling back to credentials-in-URL.'
        )
        return _basic_auth_webhook_url(url)
    expires_at = None if ttl_seconds is None else int(time.time()) + ttl_seconds
    token = webhook_url_token(url, expires_at)
    parsed = urlparse(url)
    extra = f'{WEBHOOK_TOKEN_PARAM}={token}'
    if expires_at is not None:
        extra = f'{WEBHOOK_TOKEN_EXPIRY_PARAM}={expires_at}&{extra}'
    query = f'{parsed.query}&{extra}' if parsed.query else extra
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                       parsed.params, query, parsed.fragment))


def signed_webhook_url(url: str, *, persistent: bool = False) -> str:
    """Authenticate a URL that SignalWire will later POST back to.

    Every producer — backend code and the AI agents — goes through here, so the
    scheme is switchable in one place (:func:`webhook_url_auth_mode`).

    ``persistent=True`` for a URL SignalWire STORES rather than uses once (a
    phone number's script URL, a managed swml_webhook resource): those get a
    non-expiring token, because a lapsed one would break inbound calls silently.
    Per-call callbacks leave it False and get a 12h token.

    With neither WEBHOOK_AUTH nor an internal secret configured the URL is
    returned unchanged — pairs with the soft-mode behaviour of
    :func:`app.utils.webhook_auth.require_webhook_auth`.
    """
    if webhook_url_auth_mode() == 'token':
        return _token_webhook_url(
            url,
            ttl_seconds=None if persistent else WEBHOOK_URL_TOKEN_TTL_SECONDS,
        )
    return _basic_auth_webhook_url(url)
