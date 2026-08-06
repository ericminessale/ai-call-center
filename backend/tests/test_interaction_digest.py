"""Interaction digest (R4, CONTEXT_AUDIT_2026-08-04) — one producer, three
consumers.

The digest is REGENERATED from Call rows, never patched incrementally, so
webhook ordering (call-status 'ended' vs post-prompt) cannot corrupt it:
whichever runs last simply rebuilds from a fuller database. These tests pin
the bounds (3 entries, newest first, clamped summaries), terminal-only
membership, idempotence, and the trust rule that the digest never carries
Contact.notes / custom_fields content.
"""
import json
from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.api.internal import _caller_memory
from app.models import Call, Contact, User, Workspace
from app.services.contact_enrichment import (
    DIGEST_MAX_ENTRIES,
    DIGEST_SUMMARY_CHARS,
    regenerate_interaction_digest,
)


@pytest.fixture()
def digest_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        ws = Workspace(name='Digest test')
        db.session.add(ws)
        db.session.flush()
        user = User(
            workspace_id=ws.id,
            email='agent@example.test',
            password_hash='not-used',
            name='Agent',
        )
        contact = Contact(
            workspace_id=ws.id,
            phone='+15555550100',
            display_name='Fred Example',
            first_name='Fred',
            notes='SECRET human note',
        )
        db.session.add_all([user, contact])
        db.session.commit()
        yield ws, user, contact
        db.session.remove()
        db.drop_all()


def _ended_call(ws, user, contact, sid, days_ago, reason=None, summary=None,
                disposition=None, status='ended'):
    created = datetime.utcnow() - timedelta(days=days_ago)
    call = Call(
        workspace_id=ws.id,
        user_id=user.id,
        contact_id=contact.id,
        signalwire_call_sid=sid,
        from_number=contact.phone,
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        ai_agent_name='SupportAISpecialist',
        status=status,
        created_at=created,
    )
    call.ended_at = created + timedelta(minutes=5)
    call.disposition_code = disposition
    call.summary = summary
    if reason:
        call.ai_context_dict = {'parsed_summary': {'reason': reason}}
    db.session.add(call)
    db.session.commit()
    return call


def test_digest_orders_newest_first_and_caps_entries(digest_app):
    ws, user, contact = digest_app
    for i in range(5):
        _ended_call(ws, user, contact, f'call-{i}', days_ago=10 - i,
                    reason=f'topic-{i}')

    digest = regenerate_interaction_digest(contact)

    assert len(digest) == DIGEST_MAX_ENTRIES
    # call-4 is the newest (days_ago=6), then call-3, call-2.
    assert [e['reason'] for e in digest] == ['topic-4', 'topic-3', 'topic-2']
    # Stored on the row as JSON.
    assert json.loads(contact.interaction_digest)[0]['reason'] == 'topic-4'


def test_digest_only_includes_terminal_calls(digest_app):
    ws, user, contact = digest_app
    _ended_call(ws, user, contact, 'call-done', days_ago=2, reason='done')
    _ended_call(ws, user, contact, 'call-live', days_ago=1, reason='live',
                status='ai_active')

    digest = regenerate_interaction_digest(contact)

    assert [e['reason'] for e in digest] == ['done']


def test_digest_summary_prefers_notes_and_is_clamped(digest_app):
    ws, user, contact = digest_app
    call = _ended_call(ws, user, contact, 'call-long', days_ago=1,
                       summary='raw ai summary')
    call.agent_notes = 'y' * 500
    db.session.commit()

    digest = regenerate_interaction_digest(contact)

    assert len(digest[0]['summary']) <= DIGEST_SUMMARY_CHARS
    assert digest[0]['summary'].startswith('y')


def test_digest_is_idempotent(digest_app):
    ws, user, contact = digest_app
    _ended_call(ws, user, contact, 'call-a', days_ago=1, reason='vacuum')

    first = regenerate_interaction_digest(contact)
    second = regenerate_interaction_digest(contact)

    assert first == second


def test_digest_empty_when_no_terminal_calls(digest_app):
    ws, user, contact = digest_app

    digest = regenerate_interaction_digest(contact)

    assert digest == []
    assert contact.interaction_digest is None
    assert contact.interaction_digest_list == []


def test_caller_memory_serves_the_digest(digest_app):
    ws, user, contact = digest_app
    _ended_call(ws, user, contact, 'call-old', days_ago=5, reason='warranty')
    _ended_call(ws, user, contact, 'call-new', days_ago=1, reason='vacuum suction')
    regenerate_interaction_digest(contact)

    current = Call(
        workspace_id=ws.id, user_id=user.id, contact_id=contact.id,
        signalwire_call_sid='call-now', from_number=contact.phone,
        destination='+15555550200', destination_type='phone',
        direction='inbound', handler_type='ai', status='ai_active',
    )
    db.session.add(current)
    db.session.commit()

    block, last, _cb = _caller_memory(current)

    assert [e['reason'] for e in block['interaction_digest']] == \
        ['vacuum suction', 'warranty']
    # last_interaction (the greeting source) agrees with digest[0].
    assert last['reason'] == 'vacuum suction'


def test_digest_never_leaks_notes_or_custom_fields(digest_app):
    ws, user, contact = digest_app
    contact.custom_fields_dict = {'ssn_last4': '9999'}
    _ended_call(ws, user, contact, 'call-x', days_ago=1, reason='vacuum')
    db.session.commit()

    digest = regenerate_interaction_digest(contact)

    flat = json.dumps(digest)
    assert 'SECRET' not in flat
    assert '9999' not in flat


def test_human_authored_wrapup_prose_is_not_injectable(digest_app):
    """Decision 2026-08-05: a human's wrap-up notes are written for colleagues
    (instructions, judgements, internal state) and must not reach an AI prompt
    or the history index. The AI's own prose is used instead, so no gist is
    lost — which is precisely the condition for withholding it."""
    from app.services.contact_enrichment import injectable_call_summary

    ws, user, contact = digest_app
    call = _ended_call(ws, user, contact, 'call-human-wrap', days_ago=1,
                       reason='vacuum warranty', summary='AI: discussed warranty terms.')
    call.agent_notes = 'Rude on the phone. Do NOT offer the goodwill credit again.'
    call.wrap_up_source = 'agent'
    db.session.commit()

    text = injectable_call_summary(call)

    assert 'goodwill' not in text and 'Rude' not in text
    assert text == 'AI: discussed warranty terms.'

    digest = regenerate_interaction_digest(contact)
    assert 'Rude' not in json.dumps(digest)
    # The gist still reaches the agent via reason + disposition + AI summary.
    assert digest[0]['reason'] == 'vacuum warranty'


def test_ai_authored_wrapup_prose_is_still_used(digest_app):
    """The AI's post_mortem (and the post-handoff summary appended to it) is
    the richest account of a call — keep preferring it."""
    from app.services.contact_enrichment import injectable_call_summary

    ws, user, contact = digest_app
    call = _ended_call(ws, user, contact, 'call-ai-wrap', days_ago=1,
                       reason='vacuum', summary='raw ai summary')
    call.agent_notes = 'Explained warranty; caller satisfied. After handoff: refund issued.'
    call.wrap_up_source = 'ai'
    db.session.commit()

    assert 'refund issued' in injectable_call_summary(call)


def test_human_wrapup_with_no_ai_summary_yields_no_prose(digest_app):
    """Withholding must fail closed to 'no prose', never leak as a fallback."""
    from app.services.contact_enrichment import injectable_call_summary

    ws, user, contact = digest_app
    call = _ended_call(ws, user, contact, 'call-human-only', days_ago=1,
                       reason='billing', summary=None)
    call.agent_notes = 'Internal: escalate to legal, do not discuss.'
    call.wrap_up_source = 'agent'
    db.session.commit()

    assert injectable_call_summary(call) is None
