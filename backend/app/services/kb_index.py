"""Getting knowledge-base documents into (and out of) pgvector.

A ``documents`` row is not searchable. ``search_knowledge`` reads
``chunks_{physical_name}``, which only exists once something has embedded
the document text — and until now the ONLY thing that ever did was an
operator clicking "Publish changes" in Settings → Knowledge Base. So a
freshly-migrated deployment had collections, documents (after
:mod:`app.services.knowledge_seed`), and no chunk tables: every
specialist's KB lookup returned nothing, silently, forever.

This module is the shared path. The admin endpoint, the boot task, and
workspace provisioning all reindex through :func:`reindex_collection`;
the embedding itself lives in the ai-agents service (it owns the model),
reached over its internal admin port.

Tenancy: the chunk table is keyed on ``physical_name``
(``ws{id}_{name}`` for clones, bare ``name`` for the template), never on
the per-workspace display ``name`` — otherwise every workspace's
"sales_knowledge" would reindex into the same table.

Why provisioning reindexes rather than copying rows: a clone's documents
are editable by design ("change what the AI knows" is the demo beat), so
its chunk table has to be built from ITS documents. Embedding ~19 short
documents is well under a second on a warm model, and it runs in the
background after /demo/start has already answered.
"""

from __future__ import annotations

import logging
import os
import re

from app import db

logger = logging.getLogger(__name__)

# Reindex embeds every chunk in the collection; the ai-agents side also
# cold-loads the model on first use if its pre-warm hasn't finished.
REINDEX_TIMEOUT_SECONDS = 120

# Mirrors _COLLECTION_NAME_RE in ai-agents/main_agent.py. Collection names
# reach SQL identifiers (chunks_<name>) that cannot be parameterized, so
# anything failing this never gets interpolated.
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def admin_url() -> str:
    return os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081').rstrip('/')


def chunks_table_for(physical_name: str) -> str | None:
    """``chunks_<physical_name>``, or None when the name isn't safe to
    interpolate into an identifier."""
    if not physical_name or not _SAFE_NAME_RE.match(physical_name):
        return None
    return f'chunks_{physical_name}'


def collection_chunk_count(collection) -> int:
    """Rows in a collection's chunk table: 0 when it doesn't exist yet.

    The "does the AI actually have this?" check — ``is_published`` on the
    documents is a UI flag that a failed or partial index can outlive.
    """
    table = chunks_table_for(collection.physical_name or collection.name)
    if table is None:
        return 0
    try:
        exists = db.session.execute(
            db.text('SELECT to_regclass(:t)'), {'t': table}
        ).scalar()
        if exists is None:
            return 0
        return int(db.session.execute(
            db.text(f'SELECT COUNT(*) FROM {table}')  # nosec - name is regex-gated
        ).scalar() or 0)
    except Exception as exc:
        # SQLite (tests/dev) has no to_regclass; treat as "not indexed".
        # Roll back first — on PostgreSQL a failed statement aborts the
        # transaction, and every query after it would fail too.
        db.session.rollback()
        logger.debug("chunk count for %s unavailable: %s", table, exc)
        return 0


def reindex_collection(collection) -> dict:
    """Embed a collection's documents into its chunk table.

    Returns ``{'status': ...}`` where status is one of ``ok``, ``empty``
    (nothing to index), ``unreachable``, ``timeout``, ``failed`` (the
    indexer answered non-200) or ``error``. Callers map those to HTTP
    status codes or to a retry decision; nothing here raises.
    """
    import requests as http_requests

    from app.models import Document
    from app.utils.webhook_auth import internal_service_auth

    physical_name = collection.physical_name or collection.name
    if chunks_table_for(physical_name) is None:
        return {'status': 'error', 'error': f'invalid collection name: {physical_name!r}'}

    documents = Document.query.filter_by(collection_id=collection.id).all()
    if not documents:
        return {'status': 'empty', 'documents': 0}

    payload = {
        'collection_name': physical_name,
        'documents': [{'title': d.title, 'content': d.content} for d in documents],
    }

    try:
        resp = http_requests.post(
            f'{admin_url()}/reindex',
            auth=internal_service_auth(),
            json=payload,
            timeout=REINDEX_TIMEOUT_SECONDS,
        )
    except http_requests.exceptions.ConnectionError:
        return {'status': 'unreachable'}
    except http_requests.exceptions.Timeout:
        return {'status': 'timeout'}
    except Exception as exc:
        return {'status': 'error', 'error': str(exc)}

    if resp.status_code != 200:
        return {
            'status': 'failed',
            'http_status': resp.status_code,
            'error': resp.text,
        }

    result = resp.json()
    try:
        for doc in documents:
            doc.is_published = True
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("reindex of %s indexed but publish flag failed: %s",
                     physical_name, exc)

    return {
        'status': 'ok',
        'collection': collection.name,
        'documents': len(documents),
        'chunks_indexed': result.get('chunks_indexed', 0),
    }


def index_workspace_knowledge(workspace_id, only_if_missing=True) -> dict:
    """Reindex a workspace's collections. Returns per-collection statuses.

    ``only_if_missing`` skips collections that already have chunks, which
    makes the boot task cheap on every restart after the first.
    """
    from app.models import DocumentCollection
    from app.tenancy import workspace_context

    out: dict = {}
    with workspace_context(None):
        collections = DocumentCollection.query.filter_by(
            workspace_id=workspace_id
        ).all()
        for collection in collections:
            if only_if_missing and collection_chunk_count(collection) > 0:
                out[collection.name] = 'already_indexed'
                continue
            result = reindex_collection(collection)
            out[collection.name] = result.get('status')
            if result.get('status') not in ('ok', 'empty'):
                logger.warning(
                    "[kb_index] ws %s collection '%s': %s (%s)",
                    workspace_id, collection.name, result.get('status'),
                    result.get('error', ''),
                )
    return out


# Boot retry schedule. ai-agents starts alongside the backend and
# pre-warms a sentence-transformers model before it answers, so the first
# attempts routinely land on a closed port. ~8 minutes of patience total,
# then give up: the KB stays unindexed and Settings → "Publish changes"
# still works by hand.
_BOOT_RETRY_DELAYS = (5, 15, 30, 60, 120, 240)


# One worker per boot performs the template index. Reindex is DELETE-then-
# INSERT on the chunk table, so four gunicorn workers racing it can
# interleave into duplicated chunks. Same NX-lock shape as the fabric sync
# and seat-pool boot tasks next to it.
_TEMPLATE_INDEX_LOCK_KEY = 'kb_index_template_boot_lock'
_TEMPLATE_INDEX_LOCK_TTL = 900


def start_template_indexing(app) -> None:
    """Background task: index the template workspace once ai-agents is up.

    This is what makes a fresh clone-and-own deployment work with no
    manual step — boot, and the specialists can search the seeded KB.
    Retries only while the indexer is unreachable; a real failure (bad
    payload, indexer error) is logged and not retried.
    """
    from app import socketio
    from app.services.redis_service import get_redis_client

    try:
        rc = get_redis_client()
        if rc is not None and not rc.set(
            _TEMPLATE_INDEX_LOCK_KEY, '1', nx=True, ex=_TEMPLATE_INDEX_LOCK_TTL
        ):
            logger.info("[kb_index] template indexing claimed by another worker")
            return
    except Exception as exc:
        # Redis down: proceed. Racing workers can duplicate chunks, which
        # degrades search ranking; not starting at all leaves the AI with
        # no knowledge at all. The lesser failure wins.
        logger.warning("[kb_index] lock check failed (%s) — starting unguarded", exc)

    socketio.start_background_task(_template_index_loop, app)


def _template_index_loop(app) -> None:
    from app import socketio
    from app.tenancy import DEFAULT_WORKSPACE_ID

    for attempt, delay in enumerate(_BOOT_RETRY_DELAYS):
        socketio.sleep(delay)
        try:
            with app.app_context():
                results = index_workspace_knowledge(DEFAULT_WORKSPACE_ID)
        except Exception as exc:
            logger.error("[kb_index] template indexing attempt %d failed: %s",
                         attempt + 1, exc)
            continue
        # Retry only the "indexer isn't up yet" statuses; everything else
        # is either done or won't improve by trying again.
        pending = [
            name for name, status in results.items()
            if status in ('unreachable', 'timeout')
        ]
        if not pending:
            logger.warning("[kb_index] template knowledge: %s", results)
            return
        logger.info(
            "[kb_index] indexer not ready for %s (attempt %d/%d)",
            pending, attempt + 1, len(_BOOT_RETRY_DELAYS),
        )

    logger.warning(
        "[kb_index] gave up indexing the template knowledge base — the AI's "
        "search_knowledge will find nothing until someone reindexes from "
        "Settings > Knowledge Base."
    )


def start_workspace_indexing(app, workspace_id) -> None:
    """Background task: index a freshly-provisioned workspace's clones.

    Called after /demo/start has already answered — provisioning must
    stay sub-second, and embedding is the one part of it that isn't.
    """
    from app import socketio
    socketio.start_background_task(_workspace_index_task, app, workspace_id)


def _workspace_index_task(app, workspace_id) -> None:
    try:
        with app.app_context():
            results = index_workspace_knowledge(workspace_id)
        logger.info("[kb_index] workspace %s: %s", workspace_id, results)
    except Exception as exc:
        logger.warning("[kb_index] workspace %s indexing failed: %s",
                       workspace_id, exc)


def workspace_chunk_tables(workspace_id) -> list[str]:
    """Names of the pgvector tables a workspace owns.

    Each workspace has its own ``chunks_ws{id}_*`` table per cloned
    collection, plus ``chunks_interactions_ws{id}`` for caller memory.

    Call this BEFORE deleting a workspace's rows: the collection names
    are read from ``document_collections``, which the reap deletes.
    """
    from app.services.interaction_index import interaction_collection_for_workspace

    names = {chunks_table_for(interaction_collection_for_workspace(workspace_id))}
    try:
        rows = db.session.execute(
            db.text(
                'SELECT physical_name FROM document_collections '
                'WHERE workspace_id = :ws'
            ),
            {'ws': workspace_id},
        ).fetchall()
        names.update(chunks_table_for(row[0]) for row in rows)
    except Exception as exc:
        db.session.rollback()
        logger.warning("listing chunk tables for ws %s failed: %s", workspace_id, exc)
    return sorted(n for n in names if n)


def drop_chunk_tables(names) -> int:
    """Drop chunk tables by name. Returns how many were dropped.

    Deleting a workspace's rows doesn't touch these — they're TABLES, one
    set per workspace. Without this every hosted-demo visitor permanently
    costs the database two or three of them. Best-effort throughout: a
    leaked table is untidy, a failed reap is a stuck cap slot.
    """
    dropped = 0
    for table in names:
        try:
            # Own transaction per table: one failure (a lock held by
            # another session) must not roll back the others.
            db.session.execute(db.text(f'DROP TABLE IF EXISTS {table}'))  # nosec - regex-gated
            db.session.commit()
            dropped += 1
        except Exception as exc:
            db.session.rollback()
            logger.warning("reaper: dropping %s failed: %s", table, exc)
            continue
        try:
            # Separate transaction: collection_config is created by the
            # ai-agents indexer, so it may not exist at all — and if the
            # DELETE shared the DROP's transaction, that would undo it.
            db.session.execute(
                db.text('DELETE FROM collection_config WHERE collection_name = :n'),
                {'n': table[len('chunks_'):]},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
    return dropped
