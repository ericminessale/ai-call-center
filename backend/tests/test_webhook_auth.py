import base64
from urllib.parse import parse_qs, unquote, urlparse

from flask import Flask, jsonify

from app.utils.url_utils import (
    TAP_STREAM_URL_TTL_SECONDS,
    call_context_token,
    signed_tap_stream_url,
    signed_webhook_url,
    tap_stream_signature,
    verify_call_context_token,
    verify_tap_stream_signature,
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
