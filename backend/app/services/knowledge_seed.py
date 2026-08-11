"""First-boot seed for the template workspace's knowledge base.

Both KB collections shipped empty in every deployment: migration
e5f6a7b8c9d0 created ``sales_knowledge`` and ``support_knowledge`` and
their agent assignments, but never any documents. So
``search_knowledge`` returned nothing for every specialist in every
deployment, and workspace_provision's "clone the template's collections
WITH their documents" copied two empty shells to every hosted-demo
visitor.

This seeds the documents in :mod:`app.seeds.product_knowledge` into the
TEMPLATE workspace (id 1) — the same rows migrations seed, which is what
clone-and-own runs on directly and what hosted-demo clones from. It runs
at boot rather than in a migration for two reasons: the dev/test path
that builds the schema with ``db.create_all()`` never runs migrations at
all (and so has no collections either), and content edits here should
reach existing deployments without a schema revision.

Once-only, not merely idempotent. A ``SystemConfig`` marker in the
global layer records that the seed has run, so an operator who deletes
the example documents — or rewrites them into their own product's
knowledge — does not find them resurrected on the next restart. That
distinction is the whole reason the marker exists; a
"seed if the table is empty" check would undo a deliberate deletion
every boot.

Reindexing is a separate concern: rows in ``documents`` are not
searchable until they are embedded into a ``chunks_*`` table. See
:mod:`app.services.kb_index`, which the boot path calls after this.
"""

from __future__ import annotations

import logging

from app import db
from app.models import (
    AgentCollectionAssignment,
    Document,
    DocumentCollection,
    SystemConfig,
)
from app.seeds.product_knowledge import SEED_COLLECTIONS
from app.tenancy import DEFAULT_WORKSPACE_ID, workspace_context

logger = logging.getLogger(__name__)

# Bump the suffix only if a future seed should re-run on deployments that
# already took this one — it re-adds documents an operator may have
# deleted, so treat it as a migration-grade decision.
SEED_MARKER_KEY = 'seed.product_knowledge_v1'

# Advisory-lock key for the seed. Distinct from workspace_provision's
# admission key; any int64 is fine as long as nothing else picks it.
_SEED_LOCK_KEY = 0x577C4B01


def _take_seed_lock() -> None:
    """Serialize the seed across gunicorn workers, which all boot at once.

    ``documents`` has no unique constraint, so two workers racing this
    would insert two copies of every document rather than colliding.
    Transaction-scoped, so it releases on the commit/rollback below.
    No-ops on non-PostgreSQL binds (SQLite dev/test is single-writer).
    """
    try:
        if db.session.get_bind().dialect.name != 'postgresql':
            return
        db.session.execute(
            db.text('SELECT pg_advisory_xact_lock(:key)'), {'key': _SEED_LOCK_KEY}
        )
    except Exception as exc:
        logger.warning("knowledge seed lock unavailable (%s) — proceeding unguarded", exc)


def _marker_row():
    return SystemConfig.query.filter_by(
        workspace_id=SystemConfig.GLOBAL_WORKSPACE_ID,
        key=SEED_MARKER_KEY,
    ).first()


def seed_template_knowledge() -> dict:
    """Seed the template workspace's KB collections and documents.

    Returns a summary dict for the boot log. Never raises: a failed seed
    leaves the KB empty, which is the status quo, and must not take the
    app down with it.
    """
    summary: dict = {'seeded': False}
    try:
        with workspace_context(None):
            _take_seed_lock()

            # Re-read the marker UNDER the lock: a concurrent worker may
            # have completed the whole seed while we waited for it.
            if _marker_row() is not None:
                db.session.rollback()  # release the lock, keep no writes
                return {'seeded': False, 'reason': 'already_seeded'}

            collections_created = 0
            documents_created = 0
            assignments_created = 0

            for spec in SEED_COLLECTIONS:
                collection = DocumentCollection.query.filter_by(
                    workspace_id=DEFAULT_WORKSPACE_ID, name=spec['name']
                ).first()
                if collection is None:
                    # The db.create_all() path has no collections at all —
                    # migrations are what normally create these. For the
                    # template workspace physical_name == name (§3.2);
                    # only clones get the ws{id}_ prefix.
                    collection = DocumentCollection(
                        workspace_id=DEFAULT_WORKSPACE_ID,
                        name=spec['name'],
                        physical_name=spec['name'],
                        display_name=spec['display_name'],
                        description=spec['description'],
                    )
                    db.session.add(collection)
                    db.session.flush()
                    collections_created += 1

                for agent_id in spec['agents']:
                    exists = AgentCollectionAssignment.query.filter_by(
                        workspace_id=DEFAULT_WORKSPACE_ID,
                        agent_id=agent_id,
                        collection_id=collection.id,
                    ).first()
                    if exists is None:
                        db.session.add(AgentCollectionAssignment(
                            workspace_id=DEFAULT_WORKSPACE_ID,
                            agent_id=agent_id,
                            collection_id=collection.id,
                        ))
                        assignments_created += 1

                # Never add to a collection that already has content: an
                # operator who wrote their own docs into sales_knowledge
                # before upgrading should not get our examples mixed in.
                if collection.documents.count() > 0:
                    continue

                for title, content in spec['documents']:
                    db.session.add(Document(
                        workspace_id=DEFAULT_WORKSPACE_ID,
                        collection_id=collection.id,
                        title=title,
                        # Written as an indented literal; the leading
                        # newline would otherwise open every document.
                        content=content.strip(),
                        # Only a successful reindex earns 'published' —
                        # that flag is the UI's "the AI can find this"
                        # badge, and nothing is embedded yet.
                        is_published=False,
                    ))
                    documents_created += 1

            db.session.add(SystemConfig(
                workspace_id=SystemConfig.GLOBAL_WORKSPACE_ID,
                key=SEED_MARKER_KEY,
                value='1',
            ))
            db.session.commit()

            summary = {
                'seeded': True,
                'collections_created': collections_created,
                'documents_created': documents_created,
                'assignments_created': assignments_created,
            }
    except Exception as exc:
        db.session.rollback()
        logger.error("knowledge seed failed (non-fatal): %s", exc)
        return {'seeded': False, 'error': str(exc)}

    logger.warning("[knowledge_seed] %s", summary)
    return summary
