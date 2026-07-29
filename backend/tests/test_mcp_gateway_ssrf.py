"""SSRF guard for the admin MCP-gateway probe (CRITICAL-2).

_url_host_is_safe resolves the admin-supplied gateway URL and refuses internal
targets before the backend fetches it server-side. Uses IP-literal hosts so
getaddrinfo resolves without touching the network.

_safe_get then applies that guard to EVERY hop, because requests follows
redirects by default — validating only the admin-supplied URL let a public host
bounce the probe onto an internal one.
"""
import pytest
from requests.auth import HTTPBasicAuth

from app.api.mcp_gateways import (
    _GatewayProbeError,
    _MAX_PROBE_REDIRECTS,
    _safe_get,
    _url_host_is_safe,
)


def test_blocks_loopback(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    ok, reason = _url_host_is_safe('http://127.0.0.1:8000/services')
    assert not ok
    assert '127.0.0.1' in reason


def test_blocks_ipv6_loopback(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    ok, _ = _url_host_is_safe('http://[::1]/services')
    assert not ok


def test_blocks_cloud_metadata_even_when_private_allowed(monkeypatch):
    """169.254.169.254 (link-local) must be blocked regardless of the
    private-URL flag — it's the highest-value SSRF target."""
    monkeypatch.setenv('SWML_ALLOW_PRIVATE_URLS', 'true')
    ok, reason = _url_host_is_safe('http://169.254.169.254/latest/meta-data/')
    assert not ok
    assert '169.254.169.254' in reason


def test_blocks_rfc1918_when_private_disallowed(monkeypatch):
    monkeypatch.setenv('SWML_ALLOW_PRIVATE_URLS', 'false')
    for host in ('10.0.0.5', '192.168.1.1', '172.16.0.9'):
        ok, _ = _url_host_is_safe(f'http://{host}/services')
        assert not ok, host


def test_allows_rfc1918_when_private_allowed(monkeypatch):
    """The bundled demo-mcp-gateway lives on the docker network at an RFC1918
    address, so with the flag on a private gateway URL is permitted."""
    monkeypatch.setenv('SWML_ALLOW_PRIVATE_URLS', 'true')
    ok, _ = _url_host_is_safe('http://172.18.0.4:8100/services')
    assert ok


def test_allows_public_address(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    ok, _ = _url_host_is_safe('https://93.184.216.34/services')
    assert ok


def test_rejects_missing_host(monkeypatch):
    ok, _ = _url_host_is_safe('not-a-url')
    assert not ok


def test_blocks_ipv4_mapped_ipv6_metadata(monkeypatch):
    monkeypatch.setenv('SWML_ALLOW_PRIVATE_URLS', 'true')
    ok, _ = _url_host_is_safe('http://[::ffff:169.254.169.254]/services')
    assert not ok


# ---------------------------------------------------------------------------
# _safe_get — per-hop validation
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for requests.Response (only what _safe_get reads)."""

    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {'Location': location} if location else {}
        self.is_redirect = location is not None


def _fake_requests(monkeypatch, script, seen=None):
    """Patch requests.get with a url -> _FakeResponse map; record the calls.

    Pass ``seen`` to also capture each hop's full kwargs (for the
    credential-forwarding assertions).
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if seen is not None:
            seen.append({'url': url, **kwargs})
        assert kwargs.get('allow_redirects') is False, (
            'requests must not be allowed to follow redirects itself — '
            'that is exactly what bypasses the per-hop check'
        )
        return script.get(url, _FakeResponse(200))

    monkeypatch.setattr('app.api.mcp_gateways.requests.get', fake_get)
    return calls


def test_redirect_to_cloud_metadata_is_refused(monkeypatch):
    """The bypass this exists for: a public gateway 302s to the metadata IP."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    evil = 'http://169.254.169.254/latest/meta-data/'
    calls = _fake_requests(monkeypatch, {
        'https://93.184.216.34/services': _FakeResponse(302, location=evil),
    })

    with pytest.raises(_GatewayProbeError) as exc:
        _safe_get(
            'https://93.184.216.34/services', headers={}, auth=None, timeout=1,
        )

    assert 'unsafe gateway URL' in str(exc.value)
    assert '169.254.169.254' in str(exc.value)
    # The metadata endpoint itself was never fetched.
    assert calls == ['https://93.184.216.34/services']


def test_redirect_to_rfc1918_is_refused(monkeypatch):
    monkeypatch.setenv('SWML_ALLOW_PRIVATE_URLS', 'false')
    calls = _fake_requests(monkeypatch, {
        'https://93.184.216.34/services': _FakeResponse(
            301, location='http://10.1.2.3:8000/services'),
    })

    with pytest.raises(_GatewayProbeError):
        _safe_get(
            'https://93.184.216.34/services', headers={}, auth=None, timeout=1,
        )
    assert calls == ['https://93.184.216.34/services']


def test_safe_redirect_is_followed(monkeypatch):
    """A public->public hop still works, so http->https gateways keep probing."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    calls = _fake_requests(monkeypatch, {
        'http://93.184.216.34/services': _FakeResponse(
            301, location='https://93.184.216.34/services'),
        'https://93.184.216.34/services': _FakeResponse(200),
    })

    resp = _safe_get(
        'http://93.184.216.34/services', headers={}, auth=None, timeout=1,
    )
    assert resp.status_code == 200
    assert calls == [
        'http://93.184.216.34/services',
        'https://93.184.216.34/services',
    ]


def test_relative_redirect_resolves_against_current_url(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    calls = _fake_requests(monkeypatch, {
        'https://93.184.216.34/services': _FakeResponse(
            302, location='/v2/services'),
        'https://93.184.216.34/v2/services': _FakeResponse(200),
    })

    resp = _safe_get(
        'https://93.184.216.34/services', headers={}, auth=None, timeout=1,
    )
    assert resp.status_code == 200
    assert calls[-1] == 'https://93.184.216.34/v2/services'


def test_non_http_redirect_scheme_is_refused(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    _fake_requests(monkeypatch, {
        'https://93.184.216.34/services': _FakeResponse(
            302, location='file:///etc/passwd'),
    })

    with pytest.raises(_GatewayProbeError) as exc:
        _safe_get(
            'https://93.184.216.34/services', headers={}, auth=None, timeout=1,
        )
    assert 'non-HTTP scheme' in str(exc.value)


# ---------------------------------------------------------------------------
# _safe_get — credentials must not follow a redirect off-host
# ---------------------------------------------------------------------------

_A = '93.184.216.34'   # both public, both resolve without network
_B = '93.184.216.35'


def test_credentials_are_withheld_after_cross_host_redirect(monkeypatch):
    """REGRESSION. requests strips Authorization across hosts itself
    (Session.rebuild_auth); re-issuing hops by hand bypassed that and handed
    the gateway's configured secret to whatever host it named."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    seen = []
    _fake_requests(monkeypatch, {
        f'https://{_A}/services': _FakeResponse(
            302, location=f'https://{_B}/services'),
        f'https://{_B}/services': _FakeResponse(200),
    }, seen=seen)

    resp = _safe_get(
        f'https://{_A}/services',
        headers={'Authorization': 'Bearer s3cret'},
        auth=HTTPBasicAuth('admin', 'hunter2'),
        timeout=1,
    )

    assert resp.status_code == 200
    assert len(seen) == 2
    # Hop 1 (the configured gateway) is authenticated...
    assert seen[0]['headers']['Authorization'] == 'Bearer s3cret'
    assert seen[0]['auth'] is not None
    # ...hop 2 (a host the gateway chose) gets neither secret.
    assert 'Authorization' not in seen[1]['headers']
    assert seen[1]['auth'] is None


def test_credentials_survive_same_host_https_upgrade(monkeypatch):
    """The trust boundary is the HOSTNAME, not the full origin — otherwise the
    ordinary http->https upgrade would strip auth and 401 a valid gateway."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    seen = []
    _fake_requests(monkeypatch, {
        f'http://{_A}/services': _FakeResponse(
            301, location=f'https://{_A}/services'),
        f'https://{_A}/services': _FakeResponse(200),
    }, seen=seen)

    _safe_get(
        f'http://{_A}/services',
        headers={'Authorization': 'Bearer s3cret'},
        auth=HTTPBasicAuth('admin', 'hunter2'),
        timeout=1,
    )

    assert [s['headers'].get('Authorization') for s in seen] == [
        'Bearer s3cret', 'Bearer s3cret',
    ]
    assert all(s['auth'] is not None for s in seen)


def test_caller_headers_are_not_mutated(monkeypatch):
    """Stripping must not reach back into the caller's dict — _probe_gateway
    reuses it for the per-service /tools fetches."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    _fake_requests(monkeypatch, {
        f'https://{_A}/services': _FakeResponse(
            302, location=f'https://{_B}/services'),
    })
    headers = {'Authorization': 'Bearer s3cret'}

    _safe_get(f'https://{_A}/services', headers=headers, auth=None, timeout=1)

    assert headers == {'Authorization': 'Bearer s3cret'}


def test_https_to_http_downgrade_is_refused(monkeypatch):
    """A public->public hop passes the SSRF check but must not silently drop
    off TLS, which would expose the probe (and its body) on the wire."""
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    calls = _fake_requests(monkeypatch, {
        f'https://{_A}/services': _FakeResponse(
            302, location=f'http://{_A}/services'),
    })

    with pytest.raises(_GatewayProbeError) as exc:
        _safe_get(f'https://{_A}/services', headers={}, auth=None, timeout=1)

    assert 'downgrade' in str(exc.value)
    assert calls == [f'https://{_A}/services']


def test_redirect_loop_is_bounded(monkeypatch):
    monkeypatch.delenv('SWML_ALLOW_PRIVATE_URLS', raising=False)
    url = 'https://93.184.216.34/services'
    calls = _fake_requests(monkeypatch, {url: _FakeResponse(302, location=url)})

    with pytest.raises(_GatewayProbeError) as exc:
        _safe_get(url, headers={}, auth=None, timeout=1)
    assert 'redirects' in str(exc.value)
    assert len(calls) == _MAX_PROBE_REDIRECTS + 1
