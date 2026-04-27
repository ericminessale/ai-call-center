"""
Internal endpoints — backend ⇄ ai-agents private API.

These routes exist for the ai-agents service to fetch configuration that
shapes how each agent boots (knowledge bases, MCP gateway bindings,
language profiles, etc.). They are NOT for end users; they're guarded by
:func:`app.utils.webhook_auth.require_webhook_auth` so callers must
present the shared HTTP Basic credentials configured via
``WEBHOOK_AUTH_USER`` / ``WEBHOOK_AUTH_PASSWORD``.

The same primitive that protects ``/api/webhooks/*`` and
``/api/queues/<id>/route`` protects this. The ai-agents service injects
those credentials into URLs it calls via the ``_signed_webhook_url``
helper in ``main_agent.py``.

Soft-mode behavior is the same: missing/wrong creds log a warning but
don't reject by default. Set ``WEBHOOK_AUTH_REQUIRED=true`` in the
backend ``.env`` to flip to enforce.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app.models import McpGatewayConfig
from app.utils.webhook_auth import require_webhook_auth

logger = logging.getLogger(__name__)

internal_bp = Blueprint('internal', __name__)


@internal_bp.route('/mcp-gateways', methods=['GET'])
@require_webhook_auth
def list_mcp_gateways_for_agent():
    """Return enabled MCP gateway configs for a given agent slug.

    Query params:
        agent_id (required): the agent slug (e.g. ``sales-ai``).

    Each config is returned in the shape the SDK skill expects (via
    ``McpGatewayConfig.to_skill_config()``), with cleartext credentials.
    The ai-agents service calls this once per agent at boot and feeds
    each config straight into ``agent.add_skill('mcp_gateway', cfg)``.
    """
    agent_id = (request.args.get('agent_id') or '').strip()
    if not agent_id:
        return jsonify({'error': 'agent_id query param is required'}), 400

    rows = McpGatewayConfig.query.filter_by(enabled=True).all()
    matched = [
        {
            'id': row.id,
            'name': row.name,
            'config': row.to_skill_config(),
        }
        for row in rows
        if agent_id in (row.bound_agent_ids or [])
    ]
    logger.info(
        "internal/mcp-gateways: agent_id=%s → %d gateway(s)",
        agent_id, len(matched),
    )
    return jsonify({'gateways': matched})
