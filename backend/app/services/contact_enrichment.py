"""Shared no-clobber merge of AI-learned facts into a Contact (R2,
CONTEXT_AUDIT_2026-08-04).

One projection policy for everything an AI session learns about a person.
Both writers — the queue-route transfer (mid-call) and the post-prompt
webhook (call end) — go through here so the rules can't drift apart:

- Names fill only empty or phone-placeholder displays; a human-entered
  name is never overwritten by an AI guess.
- Company fills only when absent.
- Bounded structured extras merge into ``custom_fields``.
- Prose NEVER lands in ``Contact.notes`` — that column is curated human
  knowledge (see the wrap-up migration's comment and audit R6).

Callers own the transaction; this only mutates the session object.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# LLMs told to emit "name or null" sometimes emit the *string* — never let
# those become a display name.
_JUNK_VALUES = {'null', 'none', 'unknown', 'n/a', ''}


def _clean(value):
    value = (value or '').strip()
    return '' if value.lower() in _JUNK_VALUES else value


def apply_learned_contact_fields(contact, learned, custom_extras=None):
    """Merge AI-learned identity fields into ``contact``.

    Args:
        contact: the Contact row (mutated in place, not committed).
        learned: dict with optional ``customer_name``, ``company``,
            ``caller_language``.
        custom_extras: optional dict of bounded keys merged into
            ``custom_fields`` (caller chooses the whitelist).

    Returns:
        True when anything changed.
    """
    if contact is None or not isinstance(learned, dict):
        return False
    changed = False

    customer_name = _clean(learned.get('customer_name'))
    if customer_name:
        current_display = contact.display_name or ''
        is_phone_display = (
            current_display.startswith('+') or current_display.isdigit()
        )
        if not contact.display_name or is_phone_display:
            contact.display_name = customer_name
            changed = True
        if not contact.first_name or is_phone_display:
            name_parts = customer_name.split(' ', 1)
            if name_parts[0]:
                contact.first_name = name_parts[0]
                changed = True
            if len(name_parts) >= 2 and name_parts[1]:
                contact.last_name = name_parts[1]
                changed = True

    company = _clean(learned.get('company'))
    if company and not contact.company:
        contact.company = company
        changed = True

    extras = {k: v for k, v in dict(custom_extras or {}).items() if v}
    caller_language = _clean(learned.get('caller_language'))
    if caller_language:
        extras.setdefault('caller_language', caller_language)
        # Seed the durable, human-settable language ONLY while empty. A human
        # who set it has asserted something the AI must not overwrite — and a
        # caller switching languages for one call is a per-call fact
        # (calls.caller_language), not a change of their documented language.
        if not contact.preferred_language:
            contact.preferred_language = caller_language
            changed = True
    if extras:
        existing = contact.custom_fields_dict or {}
        merged = {**existing, **extras}
        if merged != existing:
            contact.custom_fields_dict = merged
            changed = True

    return changed


# ---------------------------------------------------------------------------
# Interaction digest (R4) — one producer, three consumers.
#
# Regenerated (never incrementally patched) from Call rows, so it is
# idempotent and self-healing regardless of webhook ordering: call-status
# 'ended' and the post-prompt both call it, and whichever runs last wins with
# a strictly fuller picture. Deliberately excludes Contact.notes and
# custom_fields — the digest feeds prompt injection and screen-pops, which
# sit on the spoofable-caller-ID side of the trust line.
# ---------------------------------------------------------------------------

DIGEST_MAX_ENTRIES = 3
DIGEST_SUMMARY_CHARS = 200


def injectable_call_summary(call, limit=DIGEST_SUMMARY_CHARS):
    """Prose about one past call that is safe to put in an AI prompt.

    ``agent_notes`` is preferred ONLY when the AI authored it
    (``wrap_up_source != 'agent'``) — that value is the post-prompt's
    post_mortem, plus the post-handoff summary, so it is the richest and
    most complete account of the call and is the same trust class as
    ``Call.summary``.

    A HUMAN-authored wrap-up is deliberately withheld (decision 2026-08-05).
    It is written for colleagues, not for a voice agent: it carries
    instructions ("escalate immediately if he calls back"), judgements about
    the caller, and internal state ("waiting on legal") — none of which
    should be one paraphrase away from being spoken to whoever answers a
    spoofable phone number. Nothing is lost by withholding it, because on
    any AI-handled call ``Call.summary`` independently holds the AI's own
    prose, and ``reason`` + ``disposition`` carry the gist regardless. That
    is exactly the "only if the gist is already in context" condition.

    If humans want to steer the AI, that deserves a purpose-built field
    whose label states the contract — not a reinterpretation of the
    operational-notes field.
    """
    human_authored = (call.wrap_up_source or '') == 'agent'
    text = '' if human_authored else (call.agent_notes or '').strip()
    if not text:
        text = (call.summary or '').strip()
    if len(text) > limit:
        text = text[:limit - 3] + '...'
    return text or None


def _digest_summary(call):
    """Short prose for one digest entry (see injectable_call_summary)."""
    return injectable_call_summary(call, DIGEST_SUMMARY_CHARS)


def _digest_reason(call):
    ctx = call.ai_context_dict or {}
    parsed = ctx.get('parsed_summary')
    parsed = parsed if isinstance(parsed, dict) else {}
    return (
        _clean(parsed.get('reason'))
        or _clean(ctx.get('reason'))
        or _clean(ctx.get('issue'))
        or None
    )


def digest_entry_for_call(call):
    """One digest entry for a terminal call."""
    return {
        'call_id': call.id,
        'ended_at': call.ended_at.isoformat() if call.ended_at else None,
        'channel': 'voice',  # chat kernel adds its own value here later
        'direction': call.direction,
        'handler': call.handler_type,
        'ai_agent': call.ai_agent_name,
        'reason': _digest_reason(call),
        'disposition': call.disposition_code,
        'summary': _digest_summary(call),
    }


def regenerate_interaction_digest(contact, limit=DIGEST_MAX_ENTRIES):
    """Rebuild contact.interaction_digest from the newest terminal calls.

    Mutates the row (caller commits). Returns the digest list.
    """
    import json

    from app.models.call import Call

    if contact is None or not contact.id:
        return []
    recent = (
        Call.query.filter(
            Call.contact_id == contact.id,
            # F-02 belt: a mis-bound foreign-workspace Call (client-supplied
            # contact_id at creation) must never contribute to this
            # contact's memory, even if such a row already exists.
            Call.workspace_id == contact.workspace_id,
            Call.status.in_(Call.TERMINAL_STATUSES),
        )
        .order_by(Call.created_at.desc())
        .limit(limit)
        .all()
    )
    digest = [digest_entry_for_call(call) for call in recent]
    contact.interaction_digest = json.dumps(digest) if digest else None
    return digest


def finalize_call_memory(call):
    """THE one finalizer for caller memory at end of call (F-02/F-04/F-05).

    Workspace-checked contact resolution → stats reconcile → digest
    regeneration → history-index push, in that order. Safe to call from ANY
    terminal path, any number of times, in any webhook order: every step
    rebuilds from database truth, and non-terminal calls no-op.

    Returns the Contact on success, None otherwise. Never raises — memory
    bookkeeping must never break call teardown. Commits on success (every
    prior inline call site committed, so this preserves those semantics).
    """
    from app import db
    from app.models.call import Call
    from app.models.contact import Contact

    try:
        if call is None or call.status not in Call.TERMINAL_STATUSES:
            return None
        if not call.contact_id:
            return None
        # F-07: lock the contact — stats + digest are read-rebuild-write and
        # multiple terminal paths can finalize concurrently. SQLite no-ops.
        contact = (
            db.session.query(Contact)
            .filter_by(id=call.contact_id)
            .with_for_update()
            .first()
        )
        if contact is None:
            return None
        if contact.workspace_id != call.workspace_id:
            # F-02: never let one tenant's call write another tenant's
            # memory through a mis-bound foreign contact_id.
            logger.warning(
                "finalize_call_memory: call %s (ws %s) is bound to contact %s "
                "(ws %s) — refusing cross-workspace memory write",
                call.id, call.workspace_id, contact.id, contact.workspace_id,
            )
            return None
        contact.update_stats()
        regenerate_interaction_digest(contact)
        db.session.commit()
        # Index push AFTER the commit — it's best-effort HTTP and must not
        # hold the transaction open or roll anything back on failure.
        from app.services.interaction_index import index_call_summary
        index_call_summary(call, digest_entry_for_call(call))
        return contact
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.warning(
            "finalize_call_memory failed for call %s (non-fatal): %s",
            getattr(call, 'id', None), exc,
        )
        return None
