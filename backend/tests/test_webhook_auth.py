import base64
from urllib.parse import parse_qs, unquote, urlparse

from flask import Flask, jsonify

from app.utils.url_utils import (
    TAP_STREAM_URL_TTL_SECONDS,
    WEBHOOK_TOKEN_EXPIRY_PARAM,
    WEBHOOK_TOKEN_PARAM,
    call_context_token,
    signed_tap_stream_url,
    signed_webhook_url,
    tap_stream_signature,
    verify_call_context_token,
    verify_tap_stream_signature,
    webhook_url_auth_mode,
    webhook_url_token,
)
from app.utils.webhook_auth import require_internal_auth, require_webhook_auth


def _app_with_protected_route():
    app = Flask(__name__)

    @app.post('/webhook')
    @require_webhook_auth
    def webhook():
        return jsonify({'ok': True})

    return app


def _app_with_internal_route():
    app = Flask(__name__)

    @app.post('/internal')
    @require_internal_auth
    def internal():
        return jsonify({'ok': True})

    return app


def _basic_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def test_webhook_auth_accepts_matching_credentials(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'correct horse battery staple')
    monkeypatch.delenv('WEBHOOK_AUTH_REQUIRED', raising=False)

    response = _app_with_protected_route().test_client().post(
        '/webhook',
        headers=_basic_header('signalwire', 'correct horse battery staple'),
    )

    assert response.status_code == 200


def test_webhook_auth_rejects_missing_credentials(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'secret')
    monkeypatch.delenv('WEBHOOK_AUTH_REQUIRED', raising=False)

    response = _app_with_protected_route().test_client().post('/webhook')

    assert response.status_code == 401
    assert response.headers['WWW-Authenticate'].startswith('Basic ')


def test_webhook_auth_soft_mode_is_an_explicit_migration_escape_hatch(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'secret')
    monkeypatch.setenv('WEBHOOK_AUTH_REQUIRED', 'false')

    response = _app_with_protected_route().test_client().post('/webhook')

    assert response.status_code == 200


def test_internal_auth_rejects_leaked_webhook_creds_when_internal_secret_is_distinct(
    monkeypatch,
):
    """The CRITICAL-1 security property: WEBHOOK_AUTH creds ride semi-publicly
    in rendered SWML, so once a distinct INTERNAL_AUTH secret is configured a
    leaked WEBHOOK cred must NOT authorize the internal surface."""
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'semi-public-in-swml')
    monkeypatch.setenv('INTERNAL_AUTH_USER', 'backend')
    monkeypatch.setenv('INTERNAL_AUTH_PASSWORD', 'private-never-rendered')

    client = _app_with_internal_route().test_client()

    # Leaked webhook creds are rejected.
    assert client.post(
        '/internal', headers=_basic_header('signalwire', 'semi-public-in-swml')
    ).status_code == 401
    # The segregated internal creds are accepted.
    assert client.post(
        '/internal', headers=_basic_header('backend', 'private-never-rendered')
    ).status_code == 200


def test_internal_auth_falls_back_to_webhook_creds_when_unset(monkeypatch):
    """An unconfigured deployment (no INTERNAL_AUTH_*) behaves exactly as
    before — the internal routes accept the WEBHOOK_AUTH creds."""
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'shared-secret')
    monkeypatch.delenv('INTERNAL_AUTH_USER', raising=False)
    monkeypatch.delenv('INTERNAL_AUTH_PASSWORD', raising=False)

    assert _app_with_internal_route().test_client().post(
        '/internal', headers=_basic_header('signalwire', 'shared-secret')
    ).status_code == 200


def test_webhook_auth_is_unaffected_by_internal_creds(monkeypatch):
    """The SignalWire-facing webhook routes validate WEBHOOK_AUTH only; the
    internal creds must not authorize them."""
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'webhook-secret')
    monkeypatch.setenv('INTERNAL_AUTH_USER', 'backend')
    monkeypatch.setenv('INTERNAL_AUTH_PASSWORD', 'internal-secret')
    monkeypatch.delenv('WEBHOOK_AUTH_REQUIRED', raising=False)

    client = _app_with_protected_route().test_client()
    assert client.post(
        '/webhook', headers=_basic_header('signalwire', 'webhook-secret')
    ).status_code == 200
    assert client.post(
        '/webhook', headers=_basic_header('backend', 'internal-secret')
    ).status_code == 401


def test_call_context_token_is_keyed_on_internal_secret(monkeypatch):
    """ctk must be keyed on the segregated internal secret so a leaked
    WEBHOOK_AUTH cred can't forge another workspace's call-context token."""
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'semi-public-in-swml')
    monkeypatch.delenv('INTERNAL_AUTH_PASSWORD', raising=False)
    monkeypatch.delenv('JWT_SECRET_KEY', raising=False)
    token_fallback = call_context_token(42)

    monkeypatch.setenv('INTERNAL_AUTH_PASSWORD', 'private-never-rendered')
    token_internal = call_context_token(42)

    # Distinct internal secret ⇒ distinct token (proves it isn't keyed on the
    # leaked WEBHOOK secret), and each verifies under its own key.
    assert token_internal != token_fallback
    assert verify_call_context_token(42, token_internal)
    assert not verify_call_context_token(42, token_fallback)


def test_signed_webhook_url_encodes_credentials(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'user@example.com')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'p@ss word/with?chars')

    signed = signed_webhook_url('https://demo.example.com:8443/api/webhook?call=1')
    parsed = urlparse(signed)

    assert unquote(parsed.username or '') == 'user@example.com'
    assert unquote(parsed.password or '') == 'p@ss word/with?chars'
    assert parsed.hostname == 'demo.example.com'
    assert parsed.port == 8443
    assert parsed.path == '/api/webhook'
    assert parsed.query == 'call=1'


def test_signed_tap_stream_url_is_call_bound_and_expiring(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'tap-secret')
    issued_at = 1_700_000_000

    signed = signed_tap_stream_url(
        'wss://demo.example.com', 'call/id with spaces', now=issued_at,
    )
    parsed = urlparse(signed)
    query = parse_qs(parsed.query)

    assert parsed.path == '/ws/tap-stream/call%2Fid%20with%20spaces'
    assert query['expires'] == [str(issued_at + TAP_STREAM_URL_TTL_SECONDS)]
    assert verify_tap_stream_signature(
        'call/id with spaces',
        query['expires'][0],
        query['signature'][0],
        now=issued_at,
    )


def test_tap_stream_signature_rejects_wrong_call_expiry_and_signature(monkeypatch):
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'tap-secret')
    now = 1_700_000_000
    expires = now + 60
    signature = tap_stream_signature('call-a', expires)

    assert not verify_tap_stream_signature('call-b', expires, signature, now=now)
    assert not verify_tap_stream_signature('call-a', now - 1, signature, now=now)
    assert not verify_tap_stream_signature('call-a', expires, 'not-valid', now=now)
    assert not verify_tap_stream_signature('call-a', 'not-a-time', signature, now=now)


def test_tap_stream_signature_fails_closed_without_server_secret(monkeypatch):
    monkeypatch.delenv('WEBHOOK_AUTH_PASSWORD', raising=False)
    monkeypatch.delenv('JWT_SECRET_KEY', raising=False)

    assert not verify_tap_stream_signature('call-a', 1_700_000_060, 'guess', now=1_700_000_000)
# ---------------------------------------------------------------------------
# CRITICAL-1 Phase 2 / HIGH-4 — path-bound callback tokens instead of creds
# ---------------------------------------------------------------------------

def _token_env(monkeypatch, *, mode='token'):
    """Producers on the token scheme, with a segregated internal secret."""
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'semi-public-webhook-cred')
    monkeypatch.setenv('INTERNAL_AUTH_PASSWORD', 'server-only-internal-secret')
    monkeypatch.setenv('WEBHOOK_URL_AUTH', mode)
    monkeypatch.delenv('WEBHOOK_AUTH_REQUIRED', raising=False)


def test_token_mode_keeps_credentials_out_of_the_url(monkeypatch):
    """The whole point: a rendered SWML must stop carrying the webhook password."""
    _token_env(monkeypatch)

    url = signed_webhook_url('https://demo.example.com/api/webhooks/call-status')

    parsed = urlparse(url)
    assert parsed.username is None and parsed.password is None
    assert 'semi-public-webhook-cred' not in url
    assert WEBHOOK_TOKEN_PARAM in parse_qs(parsed.query)


def test_basic_mode_is_still_the_default(monkeypatch):
    """Default must not change — flipping the scheme is an operator decision
    that needs a live PSTN call to validate."""
    _token_env(monkeypatch, mode='basic')
    monkeypatch.delenv('WEBHOOK_URL_AUTH', raising=False)

    url = signed_webhook_url('https://demo.example.com/api/webhooks/call-status')

    assert urlparse(url).username == 'signalwire'
    assert webhook_url_auth_mode() == 'basic'


def test_a_token_url_authenticates_with_no_basic_header(monkeypatch):
    _token_env(monkeypatch)
    url = signed_webhook_url('https://demo.example.com/webhook')
    query = urlparse(url).query

    response = _app_with_protected_route().test_client().post(f'/webhook?{query}')

    assert response.status_code == 200


def test_a_token_is_bound_to_its_own_path(monkeypatch):
    """A token lifted out of a rendered SWML must not work against a different
    endpoint — that containment is what makes the leak survivable."""
    _token_env(monkeypatch)
    stolen = urlparse(
        signed_webhook_url('https://demo.example.com/api/webhooks/transcription')
    ).query

    response = _app_with_protected_route().test_client().post(f'/webhook?{stolen}')

    assert response.status_code == 401


def test_an_expired_token_is_rejected(monkeypatch):
    _token_env(monkeypatch)
    expired = webhook_url_token('http://x/webhook', 1_700_000_000)

    app = _app_with_protected_route()
    response = app.test_client().post(
        f'/webhook?{WEBHOOK_TOKEN_EXPIRY_PARAM}=1700000000&{WEBHOOK_TOKEN_PARAM}={expired}'
    )

    assert response.status_code == 401


def test_dropping_the_expiry_cannot_downgrade_a_token(monkeypatch):
    """An expiring token presented without its expiry must not validate as a
    non-expiring one — otherwise the TTL is decorative."""
    _token_env(monkeypatch)
    expiring = webhook_url_token('http://x/webhook', 1_900_000_000)

    response = _app_with_protected_route().test_client().post(
        f'/webhook?{WEBHOOK_TOKEN_PARAM}={expiring}'
    )

    assert response.status_code == 401


def test_a_bad_token_does_not_fall_back_to_basic(monkeypatch):
    """A request asserting the token scheme is judged on it. Falling through to
    Basic would let stale creds bypass a token check that just failed."""
    _token_env(monkeypatch)

    response = _app_with_protected_route().test_client().post(
        f'/webhook?{WEBHOOK_TOKEN_PARAM}=deadbeef' + '0' * 24,
        headers=_basic_header('signalwire', 'semi-public-webhook-cred'),
    )

    assert response.status_code == 401


def test_basic_callbacks_keep_working_after_the_flag_flips(monkeypatch):
    """SignalWire keeps replaying URLs it stored before the flip, so the inbound
    side has to accept both. This is what makes the rollout non-breaking."""
    _token_env(monkeypatch)

    response = _app_with_protected_route().test_client().post(
        '/webhook', headers=_basic_header('signalwire', 'semi-public-webhook-cred'),
    )

    assert response.status_code == 200


def test_tokens_are_keyed_on_the_internal_secret_not_the_webhook_cred(monkeypatch):
    """Signing with the credential we are trying to stop exposing would defeat
    the entire change."""
    _token_env(monkeypatch)
    with_internal = webhook_url_token('http://x/webhook', None)

    monkeypatch.setenv('INTERNAL_AUTH_PASSWORD', 'rotated-internal-secret')
    assert webhook_url_token('http://x/webhook', None) != with_internal


def test_persistent_urls_get_a_non_expiring_token(monkeypatch):
    """A phone number's stored callback is fetched for months; a TTL there would
    break inbound calls the moment it lapsed."""
    _token_env(monkeypatch)

    url = signed_webhook_url(
        'https://demo.example.com/api/webhooks/call-status', persistent=True,
    )

    assert WEBHOOK_TOKEN_EXPIRY_PARAM not in parse_qs(urlparse(url).query)
    assert WEBHOOK_TOKEN_PARAM in parse_qs(urlparse(url).query)


def test_token_mode_falls_back_to_basic_without_an_internal_secret(monkeypatch):
    """Never hand SignalWire a URL our own inbound check would reject."""
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'signalwire')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'pw')
    monkeypatch.setenv('WEBHOOK_URL_AUTH', 'token')
    for var in ('INTERNAL_AUTH_PASSWORD', 'JWT_SECRET_KEY'):
        monkeypatch.delenv(var, raising=False)

    url = signed_webhook_url('https://demo.example.com/webhook')

    assert urlparse(url).username == 'signalwire'
