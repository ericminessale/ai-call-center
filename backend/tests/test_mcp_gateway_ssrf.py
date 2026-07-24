"""SSRF guard for the admin MCP-gateway probe (CRITICAL-2).

_url_host_is_safe resolves the admin-supplied gateway URL and refuses internal
targets before the backend fetches it server-side. Uses IP-literal hosts so
getaddrinfo resolves without touching the network.
"""
from app.api.mcp_gateways import _url_host_is_safe


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
