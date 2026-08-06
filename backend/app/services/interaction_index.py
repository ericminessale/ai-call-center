"""Best-effort push of one call's summary into the per-workspace
interaction vector index (R5, CONTEXT_AUDIT_2026-08-04).

The index lives in pgvector via the ai-agents admin API (which owns the
embedding model). One document per terminal call, keyed by call_id
(re-posting replaces), metadata-tagged with contact_id + workspace_id so
the agent-side ``search_caller_history`` tool can hard-filter.

Strictly non-fatal and time-bounded: an unreachable indexer costs one short
timeout and a log line, never a webhook failure. The digest (R4) remains
the primary memory surface — this index only serves the long-tail "they
mentioned something from months ago" lookups.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

INDEX_TIMEOUT_SECONDS = 2


def _admin_url():
    return os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081').rstrip('/')


def interaction_collection_for_workspace(workspace_id):
    return f"interactions_ws{int(workspace_id)}"


def index_call_summary(call, entry):
    """Index one terminal call's digest entry. Returns True on success.

    ``entry`` is a ``contact_enrichment.digest_entry_for_call`` dict — the
    same bounded fields the digest stores, so the index can never carry
    more about a person than the digest does (Contact.notes and
    custom_fields stay out by construction).
    """
    import requests

    if call is None or not call.workspace_id or not call.contact_id:
        return False
    parts = []
    if entry.get('reason'):
        parts.append(f"Reason: {entry['reason']}.")
    if entry.get('disposition'):
        parts.append(f"Outcome: {entry['disposition']}.")
    if entry.get('summary'):
        parts.append(entry['summary'])
    if not parts:
        return False
    stamp = str(entry.get('ended_at') or '')[:10]
    content = (f"Call on {stamp}: " if stamp else "Call: ") + " ".join(parts)

    payload = {
        'collection_name': interaction_collection_for_workspace(call.workspace_id),
        'content': content,
        'metadata': {
            'contact_id': str(call.contact_id),
            'call_id': str(call.id),
            'workspace_id': str(call.workspace_id),
            'ended_at': str(entry.get('ended_at') or ''),
        },
    }
    try:
        from app.utils.webhook_auth import internal_service_auth
        resp = requests.post(
            f"{_admin_url()}/index-interaction",
            auth=internal_service_auth(),
            json=payload,
            timeout=INDEX_TIMEOUT_SECONDS,
        )
        if resp.ok:
            return True
        logger.info(
            "interaction index push for call %s returned HTTP %s",
            call.id, resp.status_code,
        )
    except Exception as exc:
        logger.info("interaction index push skipped for call %s: %s", call.id, exc)
    return False
