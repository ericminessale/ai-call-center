"""
Workspace provisioning — resume-or-create for hosted-demo visitors.

Replaces the persona lease (demo_lease.lease_persona). The visitor's
anonymous HttpOnly cookie token binds to a workspace via
``workspaces.session_token_hash`` (sha256 — the raw token never touches
the DB), so the binding survives Redis flushes and the 7-day idle window.

Creation seeds the workspace from the TEMPLATE workspace (id 1 — the same
rows migrations seed for clone-and-own): the 3 queues, both KB collections
WITH their documents (editable copies are the demo — "change what the AI
knows"), agent→collection assignments, MCP gateway bindings, and the
route.* config keys from the global (workspace 0) layer. Deliberately NO
simulated colleagues and NO fake history (§10.3) — the seed is structural
only. The visitor's own User row is the one user: role='visitor', generated
email, unusable password (JWTs come from /api/demo/start, never login).

The owner used to be minted as role='admin', which handed every anonymous
visitor the whole /api/admin/* surface (HIGH-3). 'visitor' keeps the
operational reach they need — see SUPERVISORY_ROLES in models/user.py — while
the admin-management routes now refuse them.

Provisioning cost is a handful of DB rows — no SignalWire API calls
(subscriber seats are leased separately when the browser needs WebRTC) —
so /demo/start stays sub-second.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    AgentCollectionAssignment,
    Document,
    DocumentCollection,
    McpGatewayConfig,
    Queue,
    SystemConfig,
    User,
    Workspace,
)
from app.models.user import ROLE_VISITOR, WORKSPACE_OWNER_ROLES
from app.services.workspace_session import (
    bump_workspace_epoch,
    end_session,
    mark_session_alive,
    touch_workspace,
    workspace_ttl_seconds,
)
from app.tenancy import DEFAULT_WORKSPACE_ID, workspace_context

logger = logging.getLogger(__name__)

# Arbitrary but fixed advisory-lock key for workspace admission. Any int64 is
# fine as long as nothing else in the schema picks the same one.
_ADMISSION_LOCK_KEY = 0x577C4C00


def _serialize_admission() -> None:
    """Serialize the MAX_WORKSPACES check against the INSERT that consumes a slot.

    The cap was raceable: every concurrent /demo/start ran its own ``live_count``
    query, all of them saw the same under-cap number, and all of them then
    created a workspace — so N simultaneous visitors could overshoot
    MAX_WORKSPACES by N-1. Counting and inserting inside one advisory lock makes
    admission strictly one-at-a-time, which is the only thing that makes the
    count meaningful; the lock is transaction-scoped, so it releases on the
    commit (or rollback) at the end of provisioning without any unlock call.

    It is held across ``_clone_templates`` too, so simultaneous first-time
    visitors provision in series rather than in parallel. That's a few hundred
    ms of DB inserts each, and correctness of a hard cap is worth more than
    concurrent cold starts on a demo box.

    No-ops on non-PostgreSQL binds (the SQLite test setup is single-writer, and
    pg_advisory_xact_lock doesn't exist there).
    """
    try:
        if db.session.get_bind().dialect.name != 'postgresql':
            return
        db.session.execute(
            text('SELECT pg_advisory_xact_lock(:key)'), {'key': _ADMISSION_LOCK_KEY}
        )
    except Exception as exc:
        # Fail OPEN: losing serialization degrades the cap to its previous
        # best-effort behaviour, which beats refusing to provision at all.
        logger.warning("workspace admission lock unavailable (%s) — cap is best-effort", exc)


def max_workspaces() -> int:
    """Global live-workspace cap — the tenancy analog of the old
    pool-exhausted 503. ``MAX_WORKSPACES``, default 200, clamp [1, 10000]."""
    raw = os.getenv('MAX_WORKSPACES', '200').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 200
    return max(1, min(n, 10000))


def _hash_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode('utf-8')).hexdigest()


def _workspace_owner(workspace):
    """The visitor's own user row for a workspace (oldest owner-role row).

    Accepts either owner role: 'visitor' is what provisioning mints now, and
    'admin' still appears on workspaces created before HIGH-3 (or after the
    migration is downgraded). A too-narrow filter here doesn't fail loudly —
    it makes resume_workspace return None, so every F5 would hand the visitor
    a brand-new workspace instead of their own.
    """
    with workspace_context(None):
        return (
            User.query
            .filter(
                User.workspace_id == workspace.id,
                User.role.in_(WORKSPACE_OWNER_ROLES),
                User.is_active.is_(True),
            )
            .order_by(User.id.asc())
            .first()
        )


def peek_workspace_id(session_token: str):
    """Read-only cookie→workspace resolution: the live workspace's int id,
    or None. No activity touch, no session-side effects — for pre-auth
    surfaces (runtime config branding) that must not extend a workspace's
    life just because its landing page was loaded.
    """
    if not session_token:
        return None
    try:
        ws = Workspace.query.filter_by(
            session_token_hash=_hash_token(session_token)
        ).first()
    except Exception:
        return None
    if ws is None or ws.id == DEFAULT_WORKSPACE_ID or not ws.is_live():
        return None
    return ws.id


def resume_workspace(session_token: str):
    """Return ``(workspace, owner_user)`` for a live cookie binding, else None.

    Read-only apart from the activity touch — the F5-restore path.
    """
    if not session_token:
        return None
    ws = Workspace.query.filter_by(session_token_hash=_hash_token(session_token)).first()
    if ws is None or ws.id == DEFAULT_WORKSPACE_ID or not ws.is_live():
        return None
    owner = _workspace_owner(ws)
    if owner is None:
        logger.error("workspace %s has no owner user — refusing resume", ws.public_id)
        return None
    touch_workspace(ws)
    return ws, owner


def provision_workspace(session_token: str):
    """Resume the cookie's workspace or create + seed a fresh one.

    Returns ``(workspace, owner_user)`` or None when the global cap is
    reached (caller answers 503, mirroring the old pool-exhausted path).
    """
    existing = resume_workspace(session_token)
    if existing is not None:
        return existing

    token_hash = _hash_token(session_token)

    with workspace_context(None):
        # Must come before the cap count below — see _serialize_admission.
        _serialize_admission()

        # Re-resolve the cookie's binding UNDER the lock before deciding it's
        # dead. The resume_workspace() above ran BEFORE we held the lock, so a
        # concurrent /demo/start for this same cookie may have committed a
        # workspace in between; that row is live, not stale. Freeing its hash
        # and provisioning another would leave two live workspaces for one
        # cookie with the first orphaned — unreachable (nothing maps to it) but
        # still holding a cap slot until its TTL.
        #
        # Serializing admission makes that MORE likely, not less: it removes
        # the duplicate-hash INSERT collision that the IntegrityError handler
        # below exists to recover from, so the second request no longer fails
        # into the resume path — it succeeds at stealing the binding.
        bound = Workspace.query.filter_by(session_token_hash=token_hash).first()
        if bound is not None and bound.id != DEFAULT_WORKSPACE_ID and bound.is_live():
            owner = _workspace_owner(bound)
            if owner is not None:
                # Commit to release the advisory lock before the activity
                # touch: touch_workspace is Redis-rate-limited and may return
                # without committing, which would pin the lock until request
                # teardown and serialize every other visitor behind us.
                db.session.commit()
                touch_workspace(bound)
                return bound, owner
            logger.error(
                "workspace %s holds this session hash but has no owner user — "
                "re-provisioning", bound.public_id,
            )

        # Genuinely dead (expired while the cookie lived on, or ownerless) —
        # free the unique binding before re-claiming it.
        if bound is not None:
            bound.session_token_hash = None
            # Emit the hash-freeing UPDATE now — flush ordering between an
            # UPDATE and a same-table INSERT is not guaranteed, and the new
            # workspace row below re-claims this exact unique value.
            db.session.flush()

        # Count only workspaces that are BOTH active AND not past their TTL.
        # A workspace's status stays ACTIVE until the reaper deletes it, so
        # without the expires_at filter expired-but-unreaped rows count toward
        # the cap and 503 new visitors between reaper runs (or forever if the
        # reaper isn't wired). Expired rows aren't live capacity — exclude them.
        live_count = (
            Workspace.query
            .filter(Workspace.status == Workspace.STATUS_ACTIVE)
            .filter(Workspace.id != DEFAULT_WORKSPACE_ID)
            .filter(Workspace.expires_at > datetime.utcnow())
            .count()
        )
    if live_count >= max_workspaces():
        logger.warning("provision_workspace: MAX_WORKSPACES (%d) reached", max_workspaces())
        # Release the admission lock (and drop the uncommitted hash-freeing
        # UPDATE, which we have no business keeping when we admit nobody)
        # instead of holding both until request teardown.
        db.session.rollback()
        return None

    now = datetime.utcnow()
    ws = Workspace(
        public_id=str(uuid.uuid4()),
        name='My Call Center',
        status=Workspace.STATUS_ACTIVE,
        session_token_hash=token_hash,
        last_active_at=now,
        expires_at=now + timedelta(seconds=workspace_ttl_seconds()),
    )

    # The whole build — including BOTH flushes (the explicit one below and
    # _clone_templates' internal one) — sits inside the try: the duplicate-
    # session_token_hash INSERT actually fires at the first flush, NOT at
    # commit, so a try around commit alone can't catch the concurrent-
    # cookie race it exists for.
    try:
        db.session.add(ws)
        db.session.flush()  # need ws.id for the clones

        owner = User(
            workspace_id=ws.id,
            email=f'owner@ws-{ws.public_id[:8]}.demo.invalid',
            name='Demo Admin',
            role=ROLE_VISITOR,
            is_active=True,
            languages=['en-US'],
            permissions={},
        )
        # Unusable password — satisfies the NOT NULL constraint; visitors only
        # ever authenticate via the /demo/start JWT mint.
        owner.set_password(secrets.token_urlsafe(32))
        db.session.add(owner)

        _clone_templates(ws)

        db.session.commit()
    except IntegrityError:
        # Two concurrent /demo/start with the same cookie both missed the
        # resume path; the unique session_token_hash made the other request
        # win. Resume what it created instead of 500ing (the loser's token
        # mint may briefly race the winner's epoch init — self-heals on the
        # next /demo/start).
        db.session.rollback()
        existing = resume_workspace(session_token)
        if existing is not None:
            return existing
        raise

    # Initialize the epoch (floored to unix-minutes) BEFORE the caller
    # mints tokens, and open the liveness fast path.
    bump_workspace_epoch(ws.public_id)
    mark_session_alive(ws.public_id)

    try:
        from app.services.demo_telemetry import bump_daily
        bump_daily('ws_created')
    except Exception:
        pass

    logger.info("provisioned workspace %s (id=%d) for session %s",
                ws.public_id, ws.id, session_token[:8])
    return ws, owner


def _clone_templates(ws) -> None:
    """Copy the template workspace's structural seed into ``ws``.

    Runs inside the provisioning transaction; every clone sets
    workspace_id explicitly (no reliance on flush-time stamping).
    """
    with workspace_context(None):
        template_queues = Queue.query.filter_by(workspace_id=DEFAULT_WORKSPACE_ID).all()
        template_collections = DocumentCollection.query.filter_by(
            workspace_id=DEFAULT_WORKSPACE_ID).all()
        template_assignments = AgentCollectionAssignment.query.filter_by(
            workspace_id=DEFAULT_WORKSPACE_ID).all()
        template_gateways = McpGatewayConfig.query.filter_by(
            workspace_id=DEFAULT_WORKSPACE_ID).all()
        template_config = SystemConfig.query.filter(
            SystemConfig.workspace_id == SystemConfig.GLOBAL_WORKSPACE_ID,
            SystemConfig.key.like('route.%'),
        ).all()

        for q in template_queues:
            db.session.add(Queue(
                workspace_id=ws.id,
                slug=q.slug,
                display_name=q.display_name,
                description=q.description,
                is_active=q.is_active,
                routing_strategy=q.routing_strategy,
                routing_transport=q.routing_transport,
                ai_agent_route=q.ai_agent_route,
                default_priority=q.default_priority,
                sla_threshold_seconds=q.sla_threshold_seconds,
                max_wait_before_ai_fallback=q.max_wait_before_ai_fallback,
            ))

        collection_id_map = {}
        for c in template_collections:
            clone = DocumentCollection(
                workspace_id=ws.id,
                name=c.name,
                # Globally-unique chunk-table / search identity per §3.2.
                physical_name=f'ws{ws.id}_{c.name}',
                display_name=c.display_name,
                description=c.description,
            )
            db.session.add(clone)
            db.session.flush()  # clone.id needed for documents + assignments
            collection_id_map[c.id] = clone.id
            for doc in c.documents.all():
                db.session.add(Document(
                    workspace_id=ws.id,
                    collection_id=clone.id,
                    title=doc.title,
                    content=doc.content,
                    is_published=doc.is_published,
                ))

        for a in template_assignments:
            new_collection_id = collection_id_map.get(a.collection_id)
            if new_collection_id is None:
                continue
            db.session.add(AgentCollectionAssignment(
                workspace_id=ws.id,
                agent_id=a.agent_id,
                collection_id=new_collection_id,
            ))

        for gw in template_gateways:
            db.session.add(McpGatewayConfig(
                workspace_id=ws.id,
                name=gw.name,
                description=gw.description,
                gateway_url=gw.gateway_url,
                auth_type=gw.auth_type,
                auth_user=gw.auth_user,
                # Same Fernet key encrypts both rows — the blob copies as-is.
                auth_password_encrypted=gw.auth_password_encrypted,
                auth_token_encrypted=gw.auth_token_encrypted,
                services_filter=gw.services_filter,
                bound_agent_ids=list(gw.bound_agent_ids or []),
                enabled=gw.enabled,
            ))

        for row in template_config:
            db.session.add(SystemConfig(
                workspace_id=ws.id,
                key=row.key,
                value=row.value,
            ))


def release_workspace(session_token: str) -> bool:
    """End a visitor's workspace: expire the row, kill its JWTs, free the
    seat + verify bindings. Idempotent; True when something was released."""
    if not session_token:
        return False
    ws = Workspace.query.filter_by(session_token_hash=_hash_token(session_token)).first()
    if ws is None or ws.id == DEFAULT_WORKSPACE_ID:
        return False

    from app.services.seat_lease import release_seat_for_user

    was_live = ws.is_live()
    with workspace_context(None):
        members = User.query.filter_by(workspace_id=ws.id).all()
    for member in members:
        try:
            release_seat_for_user(member.id)
        except Exception:
            pass
    # Verify bindings are workspace-keyed (§6.2) — one clear, not per member.
    try:
        from app.services.demo_verify import clear_bindings
        clear_bindings(ws.id)
    except Exception:
        pass

    ws.status = Workspace.STATUS_EXPIRED
    ws.session_token_hash = None  # frees the cookie binding for a fresh start
    ws.verified_number = None
    db.session.commit()

    end_session(ws.public_id)
    new_epoch = bump_workspace_epoch(ws.public_id)
    logger.info("released workspace %s (was_live=%s, new epoch=%d)",
                ws.public_id, was_live, new_epoch)
    return was_live


def ensure_default_workspace() -> None:
    """Guarantee the default/template workspace row exists (id 1).

    Migration u1v2w3x4y5z6 creates it on every migrated DB; this covers
    dev paths that build the schema via db.create_all() instead.
    """
    try:
        if db.session.get(Workspace, DEFAULT_WORKSPACE_ID) is not None:
            return
        db.session.execute(
            db.text(
                "INSERT INTO workspaces (id, public_id, name, status, created_at, last_active_at) "
                "VALUES (:id, :pid, 'My Call Center', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {'id': DEFAULT_WORKSPACE_ID, 'pid': str(uuid.uuid4())},
        )
        db.session.execute(
            db.text("SELECT setval('workspaces_id_seq', (SELECT MAX(id) FROM workspaces))")
        )
        db.session.commit()
        logger.warning("ensure_default_workspace: created missing default workspace row")
    except Exception as exc:
        db.session.rollback()
        logger.error("ensure_default_workspace failed: %s", exc)
