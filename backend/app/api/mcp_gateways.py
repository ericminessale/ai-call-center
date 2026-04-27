"""
Admin endpoints for managing MCP Gateway integrations.

Routes are registered on ``admin_bp`` so the blueprint-level admin role
gate (see ``app/api/admin.py``) covers auth automatically. Importing
this module is enough to wire the routes in — the import happens
indirectly via ``app/__init__.py`` registering ``admin_bp``.

Each :class:`McpGatewayConfig` row tells the AI agents to load the
SignalWire SDK's ``mcp_gateway`` skill against that gateway at boot.
The skill bridges MCP tools (running on the customer's gateway) into
SWAIG functions the AI can call mid-conversation.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from flask import jsonify, request
from requests.auth import HTTPBasicAuth

from app import db
from app.api import admin_bp
from app.models import McpGatewayConfig
from app.models.mcp_gateway_config import AUTH_TYPES

logger = logging.getLogger(__name__)


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
    try:
        resp = requests.get(services_url, headers=headers, auth=auth, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise _GatewayProbeError(f"Gateway unreachable: {exc}") from exc

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
    """Best-effort tool listing per service. Returns [] on failure."""
    tools_url = f"{gateway_url.rstrip('/')}/services/{service_name}/tools"
    try:
        resp = requests.get(tools_url, headers=headers, auth=auth, timeout=timeout)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        # Skill expects {"tools": [{name, description, ...}, ...]}.
        return [t.get('name') for t in (body.get('tools') or []) if isinstance(t, dict)]
    except (requests.exceptions.RequestException, ValueError):
        return []
