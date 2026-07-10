"""
Workspace tenancy core — Phase 1 of the multi-tenancy refactor.

Every tenant-owned model carries a ``workspace_id`` column and inherits the
:class:`WorkspaceScoped` marker mixin. Two SQLAlchemy event listeners
registered here make isolation the *default* instead of a per-query
discipline:

  1. ``do_orm_execute`` — every ORM SELECT issued while a workspace context
     is active gets ``workspace_id == <current>`` auto-applied to every
     WorkspaceScoped entity in the statement (``with_loader_criteria``,
     aliases included).
  2. ``before_flush`` — new WorkspaceScoped rows with no explicit
     ``workspace_id`` are stamped from the current context, derived from an
     owning row (call → user → containers), or quarantined into the default
     workspace.

Context resolution order (see :func:`current_workspace_id`):
  explicit ``workspace_context(...)`` override  >  ``flask.g.workspace_id``
  (set by the auth layers from the authenticated user / JWT ``wsid`` claim)
  >  ``None``.

``None`` context means NO filtering — deliberately fail-open. Webhooks,
Socket.IO handlers, background jobs and boot code run without a workspace
context in Phase 1 and must behave exactly as before the refactor; Phases
3–4 give those surfaces explicit contexts (call attribution, socket rooms).
Platform-level users (``users.workspace_id IS NULL`` — the operator admin)
also resolve to ``None`` and therefore see across workspaces by design.

The DEFAULT workspace (id 1, created by migration u1v2w3x4y5z6):
  - clone-and-own (TENANCY_MODE off): THE workspace — all seeded and
    user-created rows attach to it; users stay platform-level (no wsid
    claim, no filtering), so behavior is bit-for-bit today's.
  - hosted tenancy (TENANCY_MODE on): the template workspace the
    provisioner clones queues/KB/config from. Never leased to a visitor,
    unreachable by any visitor JWT, and the quarantine target for rows
    created with no resolvable workspace.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from flask import g, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session, declared_attr, with_loader_criteria

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ID = 1

_UNSET = object()
_workspace_override: ContextVar = ContextVar('workspace_override', default=_UNSET)


class WorkspaceScoped:
    """Mixin marking tenant-owned models for the auto-scope machinery.

    It exists so ``with_loader_criteria`` can target every tenant model in
    one option and so the flush-time stamper can recognize tenant rows.
    ``SystemConfig`` is intentionally NOT scoped: its ``get``/``set`` do
    explicit workspace→global fallback and the auto-filter would break the
    fallback read.

    Every concrete model defines its OWN ``workspace_id`` column (they
    differ in nullability/FK/indexing), which supersedes this declared_attr
    per declarative mixin rules. The declared_attr must still exist here:
    ``with_loader_criteria`` analyzes its lambda at construction time by
    invoking it against the MIXIN (``_WrapUserEntity`` resolves
    declared_attr via ``fget``), so a bare marker class raises
    ``AttributeError: type object 'WorkspaceScoped' has no attribute
    'workspace_id'`` on the first scoped query.
    """

    @declared_attr
    def workspace_id(cls):
        from app import db
        return db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True, index=True)


def current_workspace_id():
    """The workspace id queries should be scoped to right now, or None.

    Explicit :func:`workspace_context` override wins (background jobs,
    provisioning); otherwise the request-scoped ``g.workspace_id`` set by
    the auth layers; otherwise None (= no filtering).
    """
    override = _workspace_override.get()
    if override is not _UNSET:
        return override
    if has_app_context():
        return getattr(g, 'workspace_id', None)
    return None


@contextmanager
def workspace_context(workspace_id):
    """Explicitly pin (or clear) the workspace scope for a code block.

    ``workspace_context(ws_id)`` scopes enclosed queries to that workspace;
    ``workspace_context(None)`` forces platform scope (no filtering) even
    inside a request that has ``g.workspace_id`` set — the opt-out for
    platform-admin operations and cross-tenant jobs.
    """
    token = _workspace_override.set(workspace_id)
    try:
        yield
    finally:
        _workspace_override.reset(token)


@event.listens_for(Session, 'do_orm_execute')
def _apply_workspace_criteria(execute_state):
    """Auto-scope every ORM SELECT of WorkspaceScoped entities.

    Column/relationship loads are skipped (their parent row already passed
    the filter; re-filtering breaks lazy loads of legitimately-owned
    children). Non-SELECT statements are skipped — bulk UPDATE/DELETE (the
    demo reset wipe) must not be silently narrowed to one tenant.
    """
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
    ):
        return
    if execute_state.execution_options.get('workspace_bypass', False):
        return
    ws_id = current_workspace_id()
    if ws_id is None:
        return
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            WorkspaceScoped,
            lambda cls: cls.workspace_id == ws_id,
            include_aliases=True,
        )
    )


def _derive_workspace_id(session, obj):
    """Best-effort workspace for a new row created outside any context.

    Walks the owning-row chain: call → user/owner → containers. Only used
    when no request/explicit context exists (webhooks, sockets, background
    jobs in Phase 1). Returns None when nothing resolvable.
    """
    from app.models import Call, Conference, Contact, DocumentCollection, User

    with session.no_autoflush:
        # Owning call (Transcription, CallLeg, WebhookEvent, Callback,
        # ConferenceParticipant). NOTE: Conference.call_id is a SignalWire
        # sid STRING, not an FK — the isinstance guard skips it.
        call = getattr(obj, 'call', None)
        if call is not None and getattr(call, 'workspace_id', None):
            return call.workspace_id
        call_id = getattr(obj, 'call_id', None)
        if isinstance(call_id, int):
            call = session.get(Call, call_id)
            if call is not None and call.workspace_id:
                return call.workspace_id

        # Owning user (Call.user_id, Conference.owner_user_id). May itself
        # be platform-level (workspace NULL) — falls through.
        for attr in ('user_id', 'owner_user_id'):
            user_id = getattr(obj, attr, None)
            if isinstance(user_id, int):
                user = session.get(User, user_id)
                if user is not None and user.workspace_id:
                    return user.workspace_id

        collection_id = getattr(obj, 'collection_id', None)
        if isinstance(collection_id, int):
            coll = session.get(DocumentCollection, collection_id)
            if coll is not None and coll.workspace_id:
                return coll.workspace_id

        conference_id = getattr(obj, 'conference_id', None)
        if isinstance(conference_id, int):
            conf = session.get(Conference, conference_id)
            if conf is not None and conf.workspace_id:
                return conf.workspace_id

        contact_id = getattr(obj, 'contact_id', None)
        if isinstance(contact_id, int):
            contact = session.get(Contact, contact_id)
            if contact is not None and contact.workspace_id:
                return contact.workspace_id

    return None


@event.listens_for(Session, 'before_flush')
def _stamp_workspace_on_new(session, flush_context, instances):
    """Stamp workspace_id on new tenant rows that didn't set it explicitly.

    Users are special: NULL workspace_id is meaningful (platform-level
    user), so they are only stamped from an explicit context — never
    defaulted. Everything else falls back to the default workspace so a
    NOT NULL column can never abort a webhook/background flush; in tenancy
    mode that default is the unreachable template workspace (quarantine),
    in clone-and-own it IS the deployment's workspace.
    """
    from app.models import DocumentCollection, User

    ws_id = current_workspace_id()
    for obj in session.new:
        if not isinstance(obj, WorkspaceScoped):
            continue
        if getattr(obj, 'workspace_id', None) is None:
            if isinstance(obj, User):
                if ws_id is not None:
                    obj.workspace_id = ws_id
            else:
                obj.workspace_id = (
                    ws_id
                    or _derive_workspace_id(session, obj)
                    or DEFAULT_WORKSPACE_ID
                )
        # DocumentCollection's chunk-table/search identity must be globally
        # unique and NOT NULL; creation sites only know the display `name`.
        # ws{ID}_{name} can never collide across workspaces (and never with
        # the migrated default-workspace rows, whose physical_name = name).
        if (
            isinstance(obj, DocumentCollection)
            and not obj.physical_name
            and obj.workspace_id is not None
            and obj.name
        ):
            obj.physical_name = f'ws{obj.workspace_id}_{obj.name}'
