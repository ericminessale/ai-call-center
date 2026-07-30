"""
Admin endpoints for managing MCP Gateway integrations.

Routes are registered on ``admin_bp`` so the blueprint-level role gate
(see ``app/api/admin.py``) covers auth automatically. Importing this
module is enough to wire the routes in — the import happens indirectly
via ``app/__init__.py`` registering ``admin_bp``.

That blueprint gate admits hosted-demo visitors too (HIGH-3), so every
mutating route here — and the ``/test`` probe, which is the CRITICAL-2
SSRF surface — additionally carries ``@require_full_admin`` on top of
``@block_in_demo_mode``. Reads stay open: ``to_dict`` strips secrets.

Each :class:`McpGatewayConfig` row tells the AI agents to load the
SignalWire SDK's ``mcp_gateway`` skill against that gateway at boot.
The skill bridges MCP tools (running on the customer's gateway) into
SWAIG functions the AI can call mid-conversation.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from flask import jsonify, request
from requests.auth import HTTPBasicAuth

from app import db
from app.api import admin_bp
from app.models import McpGatewayConfig
from app.models.mcp_gateway_config import AUTH_TYPES
from app.utils.decorators import require_full_admin
from app.utils.demo_config import block_in_demo_mode

logger = logging.getLogger(__name__)


def _url_host_is_safe(url: str) -> tuple[bool, str]:
    """SSRF guard: resolve the URL's host and reject internal targets.

    The gateway URL is admin-supplied and the backend fetches it server-side
    (:func:`_probe_gateway`), so without this an admin could point it at
    internal services or the cloud-metadata endpoint and read the reflected
    response (classic SSRF). It was reachable by any hosted-demo visitor when
    visitors were provisioned as workspace admins; they no longer are (HIGH-3)
    and the routes are role-gated, but this guard is the one that has to hold
    for a clone-and-own admin too.

    ALWAYS blocks loopback, link-local (incl. the 169.254.169.254 cloud-metadata
    IP), multicast, reserved, site-local, and unspecified addresses. Private
    RFC1918 / unique-local ranges are additionally blocked unless
    ``SWML_ALLOW_PRIVATE_URLS`` is truthy — the bundled demo-mcp-gateway lives on
    the docker network at an RFC1918 address, so the demo keeps that flag on
    while the metadata endpoint stays blocked regardless.

    Checks EVERY address the host resolves to (rejects a name that resolves to
    any internal IP) and fails closed on a resolution error. Returns
    ``(ok, reason)``.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False, 'gateway_url has no host'

    allow_private = os.getenv('SWML_ALLOW_PRIVATE_URLS', '').strip().lower() == 'true'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f'could not resolve host {host!r}: {exc}'

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, f'unparseable resolved address {info[4][0]!r}'
        # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) so a mapped internal is caught.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified
                or getattr(ip, 'is_site_local', False)):
            return False, f'host {host!r} resolves to a blocked address ({ip})'
        if ip.is_private and not allow_private:
            return False, (
                f'host {host!r} resolves to a private address ({ip}); '
                'set SWML_ALLOW_PRIVATE_URLS=true to allow'
            )
    return True, ''


# Redirects are followed manually (see _safe_get) so every hop is re-checked.
# 3 is generous for a gateway that only ever needs http→https or a trailing
# slash; it exists to bound a redirect loop, not to support long chains.
_MAX_PROBE_REDIRECTS = 3

# Redirect credential policy, delegated to requests rather than reimplemented.
# Session.should_strip_auth already encodes the rule we want: keep credentials
# on the same hostname, tolerate the standard http:80 → https:443 upgrade, and
# strip on any other scheme or port change. Hand-rolling it invites exactly the
# bugs it has already absorbed — a same-host port change (443 → 8443) is a
# DIFFERENT service and must not inherit the secret, while implicit and
# explicit default ports (https://h and https://h:443) are the SAME one and
# must not trigger a strip. Delegating also means we track upstream if the
# policy is refined. The Session is a holder for that method only; no request
# is ever issued through it.
_REDIRECT_AUTH_POLICY = requests.Session()


def _safe_get(url: str, *, headers: dict, auth, timeout: float):
    """GET ``url`` with the SSRF guard applied to every hop.

    ``requests.get`` follows redirects by default, and it re-resolves each
    Location itself — so a one-time check of the admin-supplied URL proved
    nothing about where the request actually landed. A public host answering
    ``302 Location: http://169.254.169.254/latest/meta-data/`` (or any
    RFC1918 address) got fetched and its body reflected back to the admin,
    which is the whole SSRF the guard exists to stop.

    So: ``allow_redirects=False``, validate each Location, and re-issue the
    request ourselves. Raises :class:`_GatewayProbeError` on an unsafe hop, a
    relative/unparseable Location, a non-http(s) scheme, an HTTPS→HTTP
    downgrade, or too many hops.

    Credentials do NOT follow a redirect off the configured service.
    ``requests`` strips ``Authorization`` itself for the redirects it follows
    (``Session.rebuild_auth``), but re-issuing hops by hand bypasses that — so
    a gateway answering ``302 Location: https://attacker.example/`` would have
    been handed the configured Bearer token or Basic password. The decision is
    delegated to ``Session.should_strip_auth`` (see ``_REDIRECT_AUTH_POLICY``)
    so it matches upstream exactly.

    KNOWN RESIDUAL — this does not stop DNS rebinding. ``_url_host_is_safe``
    resolves the host, then ``requests`` resolves it again for the actual
    connection; a TTL-0 record can answer differently between the two. Closing
    that needs connection-level pinning of the validated IP (a custom
    HTTPAdapter), which trades away SNI/vhost correctness. Per-hop validation
    narrows the window to a single resolve pair per request instead of leaving
    redirects entirely unchecked; the route stays full-admin + non-demo gated.
    """
    current = url
    hop_headers = dict(headers)
    hop_auth = auth
    for _ in range(_MAX_PROBE_REDIRECTS + 1):
        safe, reason = _url_host_is_safe(current)
        if not safe:
            raise _GatewayProbeError(f"Refusing to probe unsafe gateway URL: {reason}")
        try:
            resp = requests.get(
                current, headers=hop_headers, auth=hop_auth, timeout=timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            raise _GatewayProbeError(f"Gateway unreachable: {exc}") from exc

        if not resp.is_redirect:
            return resp

        location = resp.headers.get('Location') or ''
        # urljoin resolves a relative Location against the current URL, so a
        # same-origin `/services/` redirect still works — and an absolute one
        # goes through _url_host_is_safe on the next pass.
        nxt = urljoin(current, location)
        cur_parsed, nxt_parsed = urlparse(current), urlparse(nxt)
        if nxt_parsed.scheme not in ('http', 'https'):
            raise _GatewayProbeError(
                f"Gateway redirected to a non-HTTP scheme: {location[:120]!r}"
            )
        # Vet the target BEFORE the transport/credential rules below, so a
        # redirect at an internal address is always refused as an unsafe host
        # rather than incidentally as (say) an HTTPS downgrade. The top of the
        # loop re-checks it a moment later; the two answer different questions
        # — "are we willing to go here?" vs "is it still safe as we connect?"
        # — and getaddrinfo is OS-cached, so the extra lookup is free.
        safe, reason = _url_host_is_safe(nxt)
        if not safe:
            raise _GatewayProbeError(f"Refusing to probe unsafe gateway URL: {reason}")
        if cur_parsed.scheme == 'https' and nxt_parsed.scheme == 'http':
            raise _GatewayProbeError(
                "Gateway redirected from HTTPS to plain HTTP — refusing to "
                "downgrade the transport mid-probe"
            )
        if _REDIRECT_AUTH_POLICY.should_strip_auth(current, nxt):
            # Off the configured service — drop the credentials rather than
            # hand them to wherever the gateway pointed. The probe usually then
            # 401s, which is the correct, visible outcome for a misconfigured
            # gateway.
            hop_headers = {
                k: v for k, v in hop_headers.items() if k.lower() != 'authorization'
            }
            hop_auth = None
            logger.warning(
                "MCP gateway probe redirected off-service (%s -> %s) — "
                "credentials withheld",
                cur_parsed.netloc, nxt_parsed.netloc,
            )
        logger.info("MCP gateway probe following redirect %s -> %s", current, nxt)
        current = nxt

    raise _GatewayProbeError(
        f"Gateway exceeded {_MAX_PROBE_REDIRECTS} redirects — "
        "configure the final URL directly"
    )


def _validate_payload(payload: dict[str, Any]) -> tuple[dict, int] | None:
    """Common validation for create/update. Returns ``None`` on success
    or a (body, status) tuple on the first failure."""
    name = (payload.get('name') or '').strip()
    if not name:
        return {'error': 'name is required'}, 400

    gateway_url = (payload.get('gateway_url') or '').strip()
    if not gateway_url:
        return {'error': 'gateway_url is required'}, 400
    if not gateway_url.startswith(('http://', 'https://')):
        return {'error': 'gateway_url must start with http:// or https://'}, 400
    safe, reason = _url_host_is_safe(gateway_url)
    if not safe:
        return {'error': f'gateway_url is not allowed: {reason}'}, 400

    auth_type = payload.get('auth_type', 'basic')
    if auth_type not in AUTH_TYPES:
        return {'error': f'auth_type must be one of {AUTH_TYPES}'}, 400

    if auth_type == 'basic' and not payload.get('auth_user'):
        return {'error': 'auth_user is required for basic auth'}, 400

    bound = payload.get('bound_agent_ids', [])
    if not isinstance(bound, list) or not all(isinstance(s, str) for s in bound):
        return {'error': 'bound_agent_ids must be a list of agent slug strings'}, 400

    services_filter = payload.get('services_filter')
    if services_filter is not None and not isinstance(services_filter, list):
        return {'error': 'services_filter must be a list (or omitted for all services)'}, 400

    return None


def _apply_payload(config: McpGatewayConfig, payload: dict[str, Any], *, is_create: bool) -> None:
    """Copy fields from a validated payload onto the model.

    Secrets only updated when the payload includes them (so updating an
    existing config without re-typing the password doesn't blank it).
    """
    config.name = payload['name'].strip()
    config.description = (payload.get('description') or '').strip() or None
    config.gateway_url = payload['gateway_url'].strip().rstrip('/')
    config.auth_type = payload.get('auth_type', 'basic')
    config.auth_user = (payload.get('auth_user') or '').strip() or None
    config.services_filter = payload.get('services_filter') or None
    config.bound_agent_ids = payload.get('bound_agent_ids', []) or []
    if 'enabled' in payload:
        config.enabled = bool(payload['enabled'])
    elif is_create:
        config.enabled = True

    # Credentials: only touch when the field is explicitly present in the
    # payload. Empty string explicitly clears.
    if 'auth_password' in payload:
        config.set_auth_password(payload['auth_password'] or None)
    if 'auth_token' in payload:
        config.set_auth_token(payload['auth_token'] or None)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@admin_bp.route('/mcp-gateways', methods=['GET'])
def list_mcp_gateways():
    """List every configured MCP gateway. Secrets are stripped."""
    configs = McpGatewayConfig.query.order_by(McpGatewayConfig.name).all()
    return jsonify({'gateways': [c.to_dict() for c in configs]})


@admin_bp.route('/mcp-gateways/<int:config_id>', methods=['GET'])
def get_mcp_gateway(config_id: int):
    config = db.session.get(McpGatewayConfig, config_id)
    if not config:
        return jsonify({'error': 'MCP gateway not found'}), 404
    return jsonify(config.to_dict())


@admin_bp.route('/mcp-gateways', methods=['POST'])
@block_in_demo_mode
@require_full_admin
def create_mcp_gateway():
    payload = request.get_json() or {}
    invalid = _validate_payload(payload)
    if invalid is not None:
        return jsonify(invalid[0]), invalid[1]

    config = McpGatewayConfig()
    _apply_payload(config, payload, is_create=True)
    db.session.add(config)
    db.session.commit()
    logger.info(
        "Created MCP gateway %s (id=%s, url=%s, agents=%s)",
        config.name, config.id, config.gateway_url, config.bound_agent_ids,
    )
    return jsonify(config.to_dict()), 201


@admin_bp.route('/mcp-gateways/<int:config_id>', methods=['PUT'])
@block_in_demo_mode
@require_full_admin
def update_mcp_gateway(config_id: int):
    config = db.session.get(McpGatewayConfig, config_id)
    if not config:
        return jsonify({'error': 'MCP gateway not found'}), 404

    payload = request.get_json() or {}
    invalid = _validate_payload(payload)
    if invalid is not None:
        return jsonify(invalid[0]), invalid[1]

    _apply_payload(config, payload, is_create=False)
    db.session.commit()
    logger.info("Updated MCP gateway %s (id=%s)", config.name, config.id)
    return jsonify(config.to_dict())


@admin_bp.route('/mcp-gateways/<int:config_id>', methods=['DELETE'])
@block_in_demo_mode
@require_full_admin
def delete_mcp_gateway(config_id: int):
    config = db.session.get(McpGatewayConfig, config_id)
    if not config:
        return jsonify({'error': 'MCP gateway not found'}), 404
    name = config.name
    db.session.delete(config)
    db.session.commit()
    logger.info("Deleted MCP gateway %s (id=%s)", name, config_id)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


@admin_bp.route('/mcp-gateways/<int:config_id>/test', methods=['POST'])
@block_in_demo_mode
@require_full_admin
def test_mcp_gateway(config_id: int):
    """Probe the configured gateway and return the services it exposes.

    Hits the gateway's ``/services`` endpoint with the configured
    credentials. Returns the service list plus, for each service, the
    list of tool names — exactly what the admin needs to confirm
    "yes, this gateway is reachable and exposes what I expected."
    """
    config = db.session.get(McpGatewayConfig, config_id)
    if not config:
        return jsonify({'error': 'MCP gateway not found'}), 404

    try:
        services = _probe_gateway(config)
    except _GatewayProbeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 200
    return jsonify({'ok': True, 'services': services})


class _GatewayProbeError(RuntimeError):
    """Probe failed for a reason worth surfacing to the admin UI."""


def _probe_gateway(config: McpGatewayConfig, *, timeout: float = 8.0) -> list[dict]:
    """List services + tools from the gateway. Raises on failure."""
    headers: dict[str, str] = {}
    auth = None
    if config.auth_type == 'bearer':
        token = config.get_auth_token()
        if not token:
            raise _GatewayProbeError("Bearer token not configured")
        headers['Authorization'] = f'Bearer {token}'
    elif config.auth_type == 'basic':
        password = config.get_auth_password()
        if not config.auth_user or password is None:
            raise _GatewayProbeError("Basic auth credentials not configured")
        auth = HTTPBasicAuth(config.auth_user, password)

    services_url = f"{config.gateway_url.rstrip('/')}/services"
    # Re-validate at fetch time: a stored row may predate the create-time guard.
    # _safe_get does that check on this URL and on every redirect hop, so a
    # public host can't bounce the probe onto an internal address.
    resp = _safe_get(services_url, headers=headers, auth=auth, timeout=timeout)

    if resp.status_code == 401:
        raise _GatewayProbeError("Gateway rejected credentials (401)")
    if resp.status_code >= 400:
        raise _GatewayProbeError(
            f"Gateway returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        services_payload = resp.json()
    except ValueError as exc:
        raise _GatewayProbeError(f"Gateway response was not JSON: {exc}") from exc

    # Per the SDK skill: GET /services returns a dict keyed by service name.
    if not isinstance(services_payload, dict):
        raise _GatewayProbeError("Gateway /services response was not a JSON object")

    out: list[dict] = []
    for service_name, meta in services_payload.items():
        tools = _probe_service_tools(
            config.gateway_url, service_name, headers, auth, timeout
        )
        out.append({
            'name': service_name,
            'description': (meta or {}).get('description') if isinstance(meta, dict) else None,
            'enabled': (meta or {}).get('enabled', True) if isinstance(meta, dict) else True,
            'tools': tools,
        })
    return out


def _probe_service_tools(
    gateway_url: str,
    service_name: str,
    headers: dict,
    auth,
    timeout: float,
) -> list[str]:
    """Best-effort tool listing per service. Returns [] on failure.

    Goes through _safe_get for its own SSRF check: this fetch has the same
    host as /services but a different path, so it can be redirected
    independently — the /services hop being clean says nothing about it.
    """
    tools_url = f"{gateway_url.rstrip('/')}/services/{service_name}/tools"
    try:
        resp = _safe_get(tools_url, headers=headers, auth=auth, timeout=timeout)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        # Skill expects {"tools": [{name, description, ...}, ...]}.
        return [t.get('name') for t in (body.get('tools') or []) if isinstance(t, dict)]
    except (_GatewayProbeError, requests.exceptions.RequestException, ValueError):
        return []
