"""
Workspace reaper for the hosted demo deployment (Phase 3, §4.2).

Replaces the Phase 1 whole-floor nightly wipe. Two entry points, both
invoked over HTTP by the ``demo-reset`` cron container:

  - :func:`reap_expired_workspaces` — HOURLY GC. Every workspace whose
    lifetime has lapsed (``status != active`` or ``expires_at`` passed)
    is deleted: its rows in dependency order, its ``ws:{id}:*`` Redis
    keys, its verify bindings and seat leases; its JWT epoch is bumped
    so surviving tokens die instantly. Live workspaces are untouched —
    a returning visitor keeps their 7-day workspace across reaper runs.

  - :func:`nightly_safety_pass` — the same GC, plus: cap total live
    workspaces at ``MAX_WORKSPACES`` (reap oldest-idle beyond the cap)
    and clear interaction rows that quarantined into the template
    workspace (unattributed webhook traffic lands there and would
    otherwise accumulate forever).

NO FLUSHDB — ever. The shared Redis now holds every live workspace's
queue state, verify bindings and epochs, plus the Socket.IO message
queue; a flush would sever realtime for every connected visitor. All
Redis cleanup is per-workspace pattern deletes.

Refuses outside hosted-demo mode — a self-hosted deployment's single
workspace must never be GC'd.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from app import db, socketio
from app.models import (
    AgentCollectionAssignment,
    Call,
    CallLeg,
    Callback,
    Conference,
    ConferenceParticipant,
    Contact,
    Document,
    DocumentCollection,
    McpGatewayConfig,
    Queue,
    QueueAgentAssignment,
    SystemConfig,
    Transcription,
    User,
    WebhookEvent,
    Workspace,
)
from app.services.redis_service import get_redis_client
from app.services.ws_rooms import workspace_room, ws_clients_key
from app.services.workspace_session import bump_workspace_epoch, end_session
from app.tenancy import DEFAULT_WORKSPACE_ID
from app.utils.demo_config import is_demo_mode

logger = logging.getLogger(__name__)


def max_workspaces() -> int:
    """Total live-workspace cap enforced by the nightly pass."""
    raw = os.getenv('MAX_WORKSPACES', '200').strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 200


# Per-workspace deletion order, children first. Interaction rows, then the
# cloned resources, then config/users/workspace. Every model here carries
# workspace_id (WorkspaceScoped); ``callbacks`` is included explicitly (it
# was a known miss in the pre-tenancy wipe). QueueAgentAssignment is NOT
# here — it has no workspace_id column; it's deleted via queue/user
# subqueries in reap_workspace before its parents go.
_REAP_MODELS_IN_ORDER = (
    Transcription,
    CallLeg,
    ConferenceParticipant,
    Conference,
    WebhookEvent,
    Callback,
    Call,
    Contact,
    Document,
    AgentCollectionAssignment,
    DocumentCollection,
    Queue,
    McpGatewayConfig,
)

# Interaction rows only — cleared from the TEMPLATE workspace by the
# nightly pass (quarantined webhook traffic). The template's queues, KB
# and config are the clone source and must survive.
_TEMPLATE_INTERACTION_MODELS = (
    Transcription,
    CallLeg,
    ConferenceParticipant,
    Conference,
    WebhookEvent,
    Callback,
    Call,
    Contact,
)


def _delete_workspace_redis_keys(ws_id: int) -> int:
    """Pattern-delete a workspace's ``ws:{id}:*`` keys + its socket set."""
    redis_client = get_redis_client()
    if redis_client is None:
        return 0
    deleted = 0
    try:
        for raw_key in redis_client.scan_iter(f'ws:{ws_id}:*'):
            redis_client.delete(raw_key)
            deleted += 1
        redis_client.delete(ws_clients_key(ws_id))
    except Exception as exc:
        logger.warning("reaper: redis cleanup for ws %s failed: %s", ws_id, exc)
    return deleted


def reap_workspace(ws: Workspace) -> dict[str, Any]:
    """GC one workspace. Returns per-table delete counts for the log.

    Order matters:
      1. epoch bump + session end — kills the workspace's JWTs first, so
         nothing can race new writes into rows we're deleting;
      2. seat release + verify-binding clears per member (user rows still
         exist here);
      3. DB rows in dependency order, config layer, users, workspace row;
      4. Redis ``ws:{id}:*`` pattern delete (queue state, outbound cap);
      5. a ``demo:reset`` nudge to the workspace room so any lingering tab
         (cookie-resume in another window) reloads to the landing page.
    """
    from app.services.seat_lease import release_seat_for_user

    counts: dict[str, Any] = {'workspace': ws.public_id}
    ws_id = ws.id
    public_id = ws.public_id

    bump_workspace_epoch(public_id)
    end_session(public_id)

    members = User.query.filter(User.workspace_id == ws_id).all()
    redis_client = get_redis_client()
    # Verify bindings are workspace-keyed (§6.2) — clear once, BEFORE the
    # ws:{id}:* pattern delete removes the reverse keys the clear reads.
    try:
        from app.services.demo_verify import clear_bindings
        clear_bindings(ws_id)
    except Exception:
        pass
    for member in members:
        try:
            release_seat_for_user(member.id)
        except Exception:
            pass
        # Agent-status residue: the agents:{status} sets have no TTL and
        # user ids are never reused, so a reaped member left in one (e.g.
        # backend restarted while they were 'available', so no disconnect
        # cleanup ran) would be a permanent per-reap leak.
        if redis_client is not None:
            try:
                for status in ('available', 'busy', 'after-call', 'break', 'offline'):
                    redis_client.srem(f'agents:{status}', str(member.id))
                redis_client.delete(f'agent:{member.id}')
                redis_client.delete(f'agent_last_assigned:{member.id}')
            except Exception:
                pass

    try:
        # Queue opt-ins first: no workspace_id column, so scope by parent
        # queue OR member user (both FKs are ondelete=CASCADE, but explicit
        # deletion keeps the counts honest and doesn't lean on the DDL).
        ws_queue_ids = db.session.query(Queue.id).filter(Queue.workspace_id == ws_id)
        ws_user_ids = db.session.query(User.id).filter(User.workspace_id == ws_id)
        counts['queue_agent_assignments'] = (
            db.session.query(QueueAgentAssignment)
            .filter(
                QueueAgentAssignment.queue_id.in_(ws_queue_ids)
                | QueueAgentAssignment.user_id.in_(ws_user_ids)
            )
            .delete(synchronize_session=False)
        )
        # Cross-workspace contact references: calls.contact_id has NO
        # ondelete, and the unscoped phone lookup in webhook ingress can
        # bind another workspace's call to a contact owned here. Left in
        # place, the Contact delete below FK-aborts and rolls back the
        # whole reap. SET NULL beats keeping a pointer into deleted data.
        db.session.execute(db.text(
            "UPDATE calls SET contact_id = NULL WHERE contact_id IN "
            "(SELECT id FROM contacts WHERE workspace_id = :ws) "
            "AND workspace_id IS DISTINCT FROM :ws"
        ), {'ws': ws_id})
        for model in _REAP_MODELS_IN_ORDER:
            counts[model.__tablename__] = (
                db.session.query(model)
                .filter(model.workspace_id == ws_id)
                .delete(synchronize_session=False)
            )
        counts['system_config'] = (
            db.session.query(SystemConfig)
            .filter(SystemConfig.workspace_id == ws_id)
            .delete(synchronize_session=False)
        )
        # Clear FK references that outlive the members (e.g. a visitor
        # touched a global config row — shouldn't happen, but SET NULL
        # beats an FK abort).
        db.session.execute(db.text(
            "UPDATE system_config SET updated_by = NULL WHERE updated_by IN "
            "(SELECT id FROM users WHERE workspace_id = :ws)"
        ), {'ws': ws_id})
        counts['users'] = (
            db.session.query(User)
            .filter(User.workspace_id == ws_id)
            .delete(synchronize_session=False)
        )
        db.session.delete(ws)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("reaper: DB delete for workspace %s failed: %s", public_id, exc)
        counts['error'] = str(exc)
        return counts

    counts['redis_keys'] = _delete_workspace_redis_keys(ws_id)

    try:
        from app.services.demo_telemetry import bump_daily
        bump_daily('ws_reaped')
    except Exception:
        pass

    try:
        socketio.emit('demo:reset', {
            'message': 'This workspace has expired — please start a new one.',
        }, room=workspace_room(ws_id))
    except Exception:
        pass

    logger.warning("reaper: reaped workspace %s (id %s): %s", public_id, ws_id, counts)
    return counts


def _expired_workspaces() -> list[Workspace]:
    now = datetime.utcnow()
    return (
        Workspace.query
        .filter(Workspace.id != DEFAULT_WORKSPACE_ID)
        .filter(
            # Released/expired status OR overdue TTL. NULL expires_at means
            # non-expiring (matches Workspace.is_live) — never reaped here.
            (Workspace.status != Workspace.STATUS_ACTIVE)
            | (Workspace.expires_at <= now)
        )
        .all()
    )


def reap_expired_workspaces() -> dict[str, Any]:
    """Hourly GC entry point. No-ops outside hosted-demo mode."""
    if not is_demo_mode():
        return {'skipped': 'hosted demo mode not set'}

    reaped = [reap_workspace(ws) for ws in _expired_workspaces()]
    return {'reaped': len(reaped), 'workspaces': reaped}


def _wipe_template_interactions() -> dict[str, int]:
    """Clear interaction rows quarantined into the template workspace."""
    counts: dict[str, int] = {}
    try:
        # Live visitor calls routinely reference quarantined ws-1 contacts
        # (webhook ingress creates the Contact with no derivable workspace
        # while the Call derives the visitor's). calls.contact_id has no
        # ondelete, so those references would FK-abort the Contact delete
        # below and roll back the whole hygiene pass. SET NULL first.
        db.session.execute(db.text(
            "UPDATE calls SET contact_id = NULL WHERE contact_id IN "
            "(SELECT id FROM contacts WHERE workspace_id = :ws) "
            "AND workspace_id IS DISTINCT FROM :ws"
        ), {'ws': DEFAULT_WORKSPACE_ID})
        for model in _TEMPLATE_INTERACTION_MODELS:
            counts[model.__tablename__] = (
                db.session.query(model)
                .filter(model.workspace_id == DEFAULT_WORKSPACE_ID)
                .delete(synchronize_session=False)
            )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("reaper: template interaction wipe failed: %s", exc)
        counts['error'] = -1
    return counts


def nightly_safety_pass() -> dict[str, Any]:
    """Nightly entry point: GC + MAX_WORKSPACES cap + template hygiene."""
    if not is_demo_mode():
        return {'skipped': 'hosted demo mode not set'}

    expired = reap_expired_workspaces()

    # Cap enforcement: reap oldest-idle live workspaces beyond the cap.
    cap = max_workspaces()
    live = (
        Workspace.query
        .filter(Workspace.id != DEFAULT_WORKSPACE_ID)
        .order_by(Workspace.last_active_at.desc())
        .all()
    )
    over_cap = live[cap:]
    capped = [reap_workspace(ws) for ws in over_cap]

    template = _wipe_template_interactions()

    # Defensive re-seed: seat pool + default workspace row (idempotent).
    seed: dict[str, Any]
    try:
        from app.services.seat_pool import ensure_seat_pool
        from app.services.workspace_provision import ensure_default_workspace
        ensure_default_workspace()
        seed = ensure_seat_pool()
    except Exception as exc:
        logger.error("reaper: nightly re-seed failed: %s", exc)
        seed = {'error': str(exc)}

    return {
        'expired': expired,
        'cap': cap,
        'live_workspaces': max(0, len(live) - len(over_cap)),
        'capped': len(capped),
        'template_interactions': template,
        'seed': seed,
    }
