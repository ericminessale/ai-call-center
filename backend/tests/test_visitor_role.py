"""HIGH-3: the hosted-demo 'visitor' role and the admin-management gate.

Three things are pinned here:

1. The role tables — 'visitor' keeps supervisory reach over its own workspace,
   reaches the /api/admin/* surface, is NOT a full admin, and can't be handed
   out through the User Management UI.
2. ``require_full_admin`` refuses a visitor and admits an admin.
3. Every route on ``admin_bp`` is classified. The expected-visitor-reachable
   set below is exhaustive, so a NEW admin route fails this test until someone
   decides whether an anonymous demo visitor may perform it. That's the
   regression this file exists for — the original hole was not a wrong check,
   it was a whole surface nobody had classified.
"""
import ast
from pathlib import Path

import pytest
from flask import Flask

from app.models.user import (
    ADMIN_SURFACE_ROLES,
    FULL_ADMIN_ROLES,
    PERMISSION_FLAGS,
    ROLE_PERMISSION_DEFAULTS,
    ROLE_VISITOR,
    SUPERVISORY_ROLES,
    WORKSPACE_OWNER_ROLES,
)
from app.utils.decorators import require_full_admin


# ---------------------------------------------------------------------------
# 1. Role tables
# ---------------------------------------------------------------------------

def test_visitor_has_supervisory_reach_but_is_not_a_full_admin():
    assert ROLE_VISITOR in SUPERVISORY_ROLES      # own floor: calls, sockets, scorecards
    assert ROLE_VISITOR in ADMIN_SURFACE_ROLES    # queues/KB/branding tabs
    assert ROLE_VISITOR not in FULL_ADMIN_ROLES   # user CRUD, gateway probe, bulk deletes
    assert FULL_ADMIN_ROLES == ('admin',)


def test_visitor_role_is_not_assignable_through_the_admin_ui():
    """Provisioning is the only thing that mints 'visitor'. If it were in
    VALID_USER_ROLES an admin could assign it — or a visitor could escalate
    off it the moment PUT /users/<id> were ever opened up."""
    from app.api.admin import VALID_USER_ROLES
    assert ROLE_VISITOR not in VALID_USER_ROLES


def test_visitor_permission_defaults_match_a_supervisor():
    """Every capability flag is a demo beat, so the visitor's ceiling is the
    supervisor's. Divergence here is what would silently break the demo."""
    visitor = ROLE_PERMISSION_DEFAULTS[ROLE_VISITOR]
    assert set(visitor) == set(PERMISSION_FLAGS)
    assert visitor == ROLE_PERMISSION_DEFAULTS['supervisor']


def test_workspace_owner_lookup_accepts_both_owner_roles():
    """resume_workspace() finds the owner by role; if it stopped matching
    'admin' every pre-migration workspace would silently fail to resume."""
    assert ROLE_VISITOR in WORKSPACE_OWNER_ROLES
    assert 'admin' in WORKSPACE_OWNER_ROLES


# ---------------------------------------------------------------------------
# 2. require_full_admin
# ---------------------------------------------------------------------------

class _FakeUser:
    def __init__(self, role):
        self.role = role


@pytest.fixture()
def flask_app():
    return Flask(__name__)


def _call_gated(flask_app, user):
    from flask import request

    @require_full_admin
    def handler():
        return 'ran', 200

    with flask_app.test_request_context('/api/admin/users'):
        if user is not None:
            request.current_user = user
        return handler()


@pytest.mark.parametrize('role', ['visitor', 'supervisor', 'agent', ''])
def test_require_full_admin_refuses_non_admins(flask_app, role):
    body, status = _call_gated(flask_app, _FakeUser(role))
    assert status == 403
    assert body.get_json()['code'] == 'admin_only'


def test_require_full_admin_admits_an_admin(flask_app):
    assert _call_gated(flask_app, _FakeUser('admin')) == ('ran', 200)


def test_require_full_admin_401s_without_a_user(flask_app):
    body, status = _call_gated(flask_app, None)
    assert status == 401


# ---------------------------------------------------------------------------
# 3. Every /api/admin/* route is classified
# ---------------------------------------------------------------------------

# Routes a hosted-demo visitor is ALLOWED to perform. Everything else on the
# blueprint must carry @require_full_admin or call _require_platform_admin().
# Read the rationale next to each handler in the source before editing this.
VISITOR_REACHABLE = {
    # AI routing + branding: SystemConfig is copy-on-write per workspace.
    'get_agent_config', 'update_agent_config',
    'get_branding', 'update_branding',
    # Queues + agent assignment — the headline routing demo.
    'list_queues', 'create_queue', 'update_queue', 'delete_queue',
    'get_queue_agents', 'update_queue_agents',
    # Knowledge base — "change what the AI knows", reindex included.
    'list_collections', 'create_collection', 'update_collection',
    'delete_collection',
    'list_documents', 'create_document', 'update_document', 'delete_document',
    'reindex_collection',
    'get_agent_assignments', 'update_agent_assignments',
    # Users: read only. Queue-agent assignment needs the roster.
    'list_users',
    # Workspace-scoped preference writes on their own row.
    'update_user_languages', 'update_user_kb_factbook_mode',
    'update_user_coach_settings',
    # Reads. list_phone_numbers self-restricts to a read-only view for
    # workspace-bound callers (no install DIDs/SIDs).
    'list_phone_numbers', 'get_webhook_url',
    'list_webhook_events', 'list_webhook_event_types',
    # MCP gateway reads only — to_dict strips secrets. Writes + the /test
    # probe (the CRITICAL-2 SSRF surface) are gated.
    'list_mcp_gateways', 'get_mcp_gateway',
}

_ADMIN_SOURCES = ('app/api/admin.py', 'app/api/mcp_gateways.py')


def _decorator_names(node):
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _admin_route_handlers():
    """{handler_name: {'decorators': set, 'platform_gated': bool}} for every
    function registered on admin_bp, read straight from the source."""
    handlers = {}
    for relative in _ADMIN_SOURCES:
        path = Path(__file__).parents[1] / relative
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorators = _decorator_names(node)
            if 'route' not in decorators:
                continue
            platform_gated = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == '_require_platform_admin'
                for inner in ast.walk(node)
            )
            handlers[node.name] = {
                'decorators': decorators,
                'platform_gated': platform_gated,
            }
    return handlers


def test_every_admin_route_is_classified():
    handlers = _admin_route_handlers()
    assert len(handlers) > 40, 'route discovery broke — no handlers found'

    unknown = VISITOR_REACHABLE - set(handlers)
    assert not unknown, f'VISITOR_REACHABLE names routes that no longer exist: {sorted(unknown)}'

    ungated = sorted(
        name for name, meta in handlers.items()
        if name not in VISITOR_REACHABLE
        and 'require_full_admin' not in meta['decorators']
        and not meta['platform_gated']
    )
    assert not ungated, (
        'These /api/admin/* routes are reachable by an anonymous hosted-demo '
        'visitor and are not classified as safe. Add @require_full_admin, or '
        f'add them to VISITOR_REACHABLE with a reason: {ungated}'
    )


def test_the_known_dangerous_routes_are_gated():
    """Spot-check the specific handlers HIGH-3 and CRITICAL-2 were about, so a
    refactor of the discovery helper above can't quietly pass this file."""
    handlers = _admin_route_handlers()
    for name in (
        'clear_calls', 'create_user', 'update_user',
        'update_user_permissions', 'delete_user',
        'create_mcp_gateway', 'update_mcp_gateway', 'delete_mcp_gateway',
        'test_mcp_gateway',
    ):
        assert 'require_full_admin' in handlers[name]['decorators'], name
    for name in (
        'sync_fabric_webhooks', 'reset_user_subscriber', 'update_phone_number',
        'demo_stats', 'list_workspaces', 'reap_workspace_now',
    ):
        assert handlers[name]['platform_gated'], name
