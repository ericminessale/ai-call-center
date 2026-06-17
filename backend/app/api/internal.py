"""
Internal endpoints — backend ⇄ ai-agents private API.

These routes exist for the ai-agents service to fetch configuration that
shapes how each agent boots (knowledge bases, MCP gateway bindings,
language profiles, etc.). They are NOT for end users; they're guarded by
:func:`app.utils.webhook_auth.require_internal_auth` so callers must
present the shared HTTP Basic credentials configured via
``WEBHOOK_AUTH_USER`` / ``WEBHOOK_AUTH_PASSWORD``.

Unlike the SignalWire-facing ``/api/webhooks/*`` surface, these routes use
:func:`require_internal_auth`, which ALWAYS enforces: missing/wrong
credentials are rejected (401) regardless of ``WEBHOOK_AUTH_REQUIRED``,
because they expose decrypted MCP gateway credentials and a destructive
demo reset. (The shared HTTP Basic creds identify the trusted ai-agents
service as a whole; per-agent authorization on the ``agent_id`` query param
is a separate follow-up — see REMEDIATION_2026-06-02.md SEC-01 overflow.)

The ai-agents service injects those credentials into URLs it calls via the
``_signed_webhook_url`` helper in ``main_agent.py``.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app import socketio
from app.models import AgentCollectionAssignment, McpGatewayConfig
from app.services.demo_reset import reset_demo_state
from app.utils.demo_config import is_demo_mode
from app.utils.webhook_auth import require_internal_auth

logger = logging.getLogger(__name__)

internal_bp = Blueprint('internal', __name__)


@internal_bp.route('/mcp-gateways', methods=['GET'])
@require_internal_auth
def list_mcp_gateways_for_agent():
    """Return enabled MCP gateway configs for a given agent slug.

    Query params:
        agent_id (required): the agent slug (e.g. ``sales-ai``). MUST be
            one of the known slugs in ``ai_control.AI_AGENTS`` — arbitrary
            values are rejected with 400.

    Each config is returned in the shape the SDK skill expects (via
    ``McpGatewayConfig.to_skill_config()``), with cleartext credentials.
    The ai-agents service calls this once per agent at boot and feeds
    each config straight into ``agent.add_skill('mcp_gateway', cfg)``.

    Trust boundary (audit clarification 2026-06-02):
        The shared HTTP Basic creds (``WEBHOOK_AUTH_USER``/``..._PASSWORD``)
        identify the ai-agents SERVICE as a single trust principal, not
        individual agents. Any holder of those creds can request any
        known agent's MCP config — this is by design: the ai-agents
        service legitimately needs to boot all five agents from one
        process. Per-agent isolation would require a per-agent identity
        infrastructure (separate tokens per agent), which is out of
        scope. The agent_id whitelist below is defense-in-depth: it
        prevents arbitrary-string injection but doesn't change the trust
        model. Tracked in REMEDIATION_2026-06-02.md.
    """
    agent_id = (request.args.get('agent_id') or '').strip()
    if not agent_id:
        return jsonify({'error': 'agent_id query param is required'}), 400

    # Whitelist against the known agent slugs. Anything else is either
    # a typo or someone probing — reject explicitly so we don't fan out
    # a DB query for arbitrary input.
    from app.api.ai_control import AI_AGENTS
    known_slugs = {a['id'] for a in AI_AGENTS}
    if agent_id not in known_slugs:
        logger.warning(
            "internal/mcp-gateways: rejected unknown agent_id=%r "
            "(not in known slugs %s)",
            agent_id, sorted(known_slugs),
        )
        return jsonify({'error': 'unknown agent_id'}), 400

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


@internal_bp.route('/agent-assignments', methods=['GET'])
@require_internal_auth
def list_agent_assignments():
    """Return all agent→knowledge-collection assignments.

    The ai-agents service polls this (30s TTL cache) from its dynamic-config
    callback so admin KB reassignments apply to new calls without a container
    restart. Returns every assignment in one response — the agent caches the
    whole map rather than fetching per-agent per-request.

    Note: this replaces the agents' old boot-time fetch of the admin-surface
    ``GET /api/admin/agent-assignments``, which sits behind user-JWT
    ``require_auth`` that the service could never satisfy — that fetch 401'd
    silently and every agent ran on its hardcoded fallback collection.
    """
    assignments = AgentCollectionAssignment.query.all()
    return jsonify({
        'assignments': [a.to_dict() for a in assignments]
    })


@internal_bp.route('/demo-reset', methods=['POST'])
@require_internal_auth
def trigger_demo_reset():
    """Run the daily demo wipe + reseed.

    Refuses outright in production (``DEMO_MODE`` unset). When DEMO
    mode is on:
      1. Truncate mutable per-day tables (calls, contacts, etc.) —
         users + queues + KB + MCP config preserved.
      2. ``FLUSHDB`` the Redis namespace (leases, queue state,
         ratelimits — all ephemeral demo state).
      3. Defensively re-run the idempotent persona seed.
      4. Broadcast a ``demo:reset`` SocketIO event so active visitor
         tabs reload cleanly rather than running on dead lease state.

    Triggered by the ``demo-reset`` cron container at 00:00 UTC.
    Available for manual operator-side testing too — just call it
    yourself (you'll need ``WEBHOOK_AUTH_USER`` / ``..._PASSWORD`` in
    the request URL). This route is destructive, so it hard-requires
    HTTP Basic auth (``require_internal_auth``) regardless of the global
    ``WEBHOOK_AUTH_REQUIRED`` flag, on top of the ``DEMO_MODE`` gate.
    """
    if not is_demo_mode():
        # Ignore silently — the cron may fire against a production
        # backend if someone misconfigures, and we'd rather no-op
        # than half-wipe.
        return jsonify({'skipped': 'DEMO_MODE not set'}), 200

    summary = reset_demo_state()
    logger.warning("demo_reset: completed: %s", summary)

    # Broadcast to every connected socket so any visitor mid-session
    # can show a "demo refreshing" toast and reload. Frontend handles
    # the UX; this is fire-and-forget on the backend.
    try:
        socketio.emit('demo:reset', {'message': 'Demo refresh — please reload'})
    except Exception as exc:
        # Reset already succeeded; broadcast failure is cosmetic.
        logger.warning("demo_reset: socket broadcast failed: %s", exc)

    return jsonify({'ok': True, 'summary': summary}), 200
