"""Workspace-scoped Socket.IO rooms + Redis key prefixes (Phase 3, §8).

One room per workspace — ``ws:{workspace_id}`` — auto-joined server-side at
connect/authenticate from the token's user row, so both parallel frontend
sockets get it for free. Every emit that used to be a room-less global
broadcast now targets the producing row's workspace room instead; in
clone-and-own (single implicit workspace, platform-level users) everything
resolves to the one constant room ``ws:1`` and the floor behaves as before.

Tenant-owned Redis keys get the matching ``ws:{id}:`` prefix
(``ws:{id}:queue:{slug}``, ``ws:{id}:queue_agents:{slug}``,
``ws:{id}:round_robin:{slug}``, ``ws:{id}:outbound``) so slug-keyed queue
state can never collide across workspaces. Call-sid-keyed keys, agent-id
keys, locks and rate limits stay unprefixed — their identifiers are already
globally unique (§8.2).

Naming note: ``workspace_session.py`` owns ``ws:epoch:{public_id}`` /
``ws:session:{public_id}`` / ``ws:touch:{public_id}`` — keyed by the UUID
public_id, so they can't collide with the integer-id ``ws:{id}:*`` namespace
here, and the reaper's ``ws:{id}:*`` pattern delete can't touch them.
"""

from __future__ import annotations

from app.tenancy import DEFAULT_WORKSPACE_ID

# Redis set of socket sids per workspace — maintained by the connect/
# authenticate/disconnect handlers, consumed by the queue-monitor wallboard
# so it only computes stats for workspaces somebody is actually watching.
WS_CLIENTS_PREFIX = 'ws_clients:'


def _ws_int(workspace_id) -> int:
    """Normalize to a concrete workspace int id. ``None`` (platform users,
    legacy rows) maps to the default workspace — in clone-and-own that IS
    the deployment's workspace; in hosted mode it is the template/quarantine
    workspace, which no visitor token can reach."""
    return int(workspace_id) if workspace_id else DEFAULT_WORKSPACE_ID


def workspace_room(workspace_id=None) -> str:
    """Socket.IO room name for a workspace."""
    return f'ws:{_ws_int(workspace_id)}'


def ws_key(workspace_id, suffix: str) -> str:
    """Tenant-owned Redis key: ``ws:{id}:{suffix}``."""
    return f'ws:{_ws_int(workspace_id)}:{suffix}'


def ws_clients_key(workspace_id) -> str:
    """Redis set holding the live socket sids of a workspace's clients."""
    return f'{WS_CLIENTS_PREFIX}{_ws_int(workspace_id)}'


def active_socket_workspace_ids(redis_client) -> list[int]:
    """Workspace ids that currently have at least one connected socket.

    Scans the ``ws_clients:{id}`` sets; empty sets (all members srem'd on
    disconnect) are skipped. Used by the 5s wallboard so idle workspaces
    cost nothing.
    """
    ids: list[int] = []
    if redis_client is None:
        return ids
    for raw_key in redis_client.scan_iter(f'{WS_CLIENTS_PREFIX}*'):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        try:
            ws_id = int(key[len(WS_CLIENTS_PREFIX):])
        except (TypeError, ValueError):
            continue
        try:
            if (redis_client.scard(key) or 0) > 0:
                ids.append(ws_id)
        except Exception:
            continue
    return ids
