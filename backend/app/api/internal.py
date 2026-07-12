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

from app.models import AgentCollectionAssignment, McpGatewayConfig
from app.services.demo_reset import nightly_safety_pass, reap_expired_workspaces
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

    Tenancy: the shared agent process serves the default/template
    workspace's KB until Phase 4's per-request workspace resolution, so the
    feed is pinned to it (an unscoped query would last-wins across every
    visitor's clones). ``collection_name`` here is the SEARCH KEY the
    agents build ``chunks_{name}`` from — it must be the globally-unique
    ``physical_name`` (= display name for migrated default-workspace rows),
    matching what the reindex proxy now writes.
    """
    from app.tenancy import DEFAULT_WORKSPACE_ID
    assignments = AgentCollectionAssignment.query.filter_by(
        workspace_id=DEFAULT_WORKSPACE_ID
    ).all()
    return jsonify({
        'assignments': [
            {
                'id': a.id,
                'agent_id': a.agent_id,
                'collection_id': a.collection_id,
                'collection_name': (
                    (a.collection.physical_name or a.collection.name)
                    if a.collection else None
                ),
                'collection_display_name': (
                    a.collection.display_name if a.collection else None
                ),
            }
            for a in assignments
        ]
    })


@internal_bp.route('/workspace-gc', methods=['POST'])
@require_internal_auth
def trigger_workspace_gc():
    """Hourly workspace GC (Phase 3, §4.2 — replaces the nightly wipe).

    Reaps every EXPIRED workspace: rows in dependency order, its
    ``ws:{id}:*`` Redis keys, verify bindings, seat leases; epoch bumped
    so surviving JWTs die. Live workspaces are untouched — visitors keep
    their 7-day workspaces. NO FLUSHDB (shared Redis carries every live
    workspace's state plus the Socket.IO message queue).

    Triggered by the ``demo-reset`` cron container hourly. No-ops when
    hosted-demo mode is off. Hard-requires HTTP Basic auth
    (``require_internal_auth``) regardless of ``WEBHOOK_AUTH_REQUIRED``.
    """
    if not is_demo_mode():
        return jsonify({'skipped': 'DEMO_MODE not set'}), 200

    summary = reap_expired_workspaces()
    if summary.get('reaped'):
        logger.warning("workspace_gc: %s", summary)
    return jsonify({'ok': True, 'summary': summary}), 200


@internal_bp.route('/demo-reset', methods=['POST'])
@require_internal_auth
def trigger_demo_reset():
    """Nightly safety pass (Phase 3 — the whole-floor wipe is gone).

    Runs the same per-workspace GC as /workspace-gc, then enforces the
    ``MAX_WORKSPACES`` cap (reaping oldest-idle live workspaces beyond
    it), clears interaction rows quarantined into the template
    workspace, and defensively re-runs the idempotent seat-pool seed.
    Per-workspace ``demo:reset`` events are emitted to each reaped
    workspace's room by the reaper itself — there is deliberately no
    install-wide broadcast anymore (live visitors are unaffected).

    Triggered by the ``demo-reset`` cron container at 00:00 UTC. Kept at
    this route name so existing ops wiring keeps working. Refuses when
    hosted-demo mode is off (a self-hosted deployment's workspace must
    never be GC'd). Hard-requires HTTP Basic auth.
    """
    if not is_demo_mode():
        return jsonify({'skipped': 'DEMO_MODE not set'}), 200

    summary = nightly_safety_pass()
    logger.warning("demo_reset (nightly safety pass): completed: %s", summary)
    return jsonify({'ok': True, 'summary': summary}), 200
