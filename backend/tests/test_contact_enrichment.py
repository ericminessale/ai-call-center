"""apply_learned_contact_fields — the single projection policy for
AI-learned facts (R2, CONTEXT_AUDIT_2026-08-04).

Both writers (queue-route transfer and post-prompt webhook) flow through
this helper, so these tests pin the no-clobber contract for both at once:
AI guesses fill blanks and phone-placeholders, and never overwrite a
human-entered value. Contact.notes must stay untouched by construction —
the helper has no code path to it.
"""
import pytest
from flask import Flask

from app import db
from app.models import Contact, Workspace
from app.services.contact_enrichment import apply_learned_contact_fields


@pytest.fixture()
def enrich_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        ws = Workspace(name='Enrichment test')
        db.session.add(ws)
        db.session.commit()
        yield ws
        db.session.remove()
        db.drop_all()


def _contact(ws, **kwargs):
    defaults = dict(
        workspace_id=ws.id,
        phone='+15555550100',
        display_name='+15555550100',
        account_tier='free',
    )
    defaults.update(kwargs)
    contact = Contact(**defaults)
    db.session.add(contact)
    db.session.commit()
    return contact


def test_fills_phone_placeholder_display(enrich_app):
    contact = _contact(enrich_app)

    changed = apply_learned_contact_fields(
        contact, {'customer_name': 'Fred Example'},
    )

    assert changed
    assert contact.display_name == 'Fred Example'
    assert contact.first_name == 'Fred'
    assert contact.last_name == 'Example'


def test_never_clobbers_human_entered_name(enrich_app):
    contact = _contact(
        enrich_app,
        display_name='Fredrick Example III',
        first_name='Fredrick',
        last_name='Example III',
    )

    changed = apply_learned_contact_fields(
        contact, {'customer_name': 'Fred'},
    )

    assert not changed
    assert contact.display_name == 'Fredrick Example III'
    assert contact.first_name == 'Fredrick'


def test_rejects_llm_junk_name_strings(enrich_app):
    contact = _contact(enrich_app)

    for junk in ('null', 'None', 'UNKNOWN', 'n/a', '  ', None):
        assert not apply_learned_contact_fields(
            contact, {'customer_name': junk},
        )
    assert contact.display_name == '+15555550100'
    assert contact.first_name is None


def test_company_fills_only_when_absent(enrich_app):
    contact = _contact(enrich_app, company=None)

    assert apply_learned_contact_fields(contact, {'company': 'Vacuums R Us'})
    assert contact.company == 'Vacuums R Us'

    assert not apply_learned_contact_fields(contact, {'company': 'Other Corp'})
    assert contact.company == 'Vacuums R Us'


def test_caller_language_and_extras_merge_into_custom_fields(enrich_app):
    contact = _contact(enrich_app)
    contact.custom_fields_dict = {'existing': 'kept'}
    db.session.commit()

    changed = apply_learned_contact_fields(
        contact,
        {'caller_language': 'es-ES'},
        custom_extras={'department': 'support', 'urgency': ''},
    )

    assert changed
    fields = contact.custom_fields_dict
    assert fields['existing'] == 'kept'
    assert fields['caller_language'] == 'es-ES'
    assert fields['department'] == 'support'
    # Falsy extras are dropped, not stored as empty strings.
    assert 'urgency' not in fields


def test_no_change_returns_false(enrich_app):
    contact = _contact(enrich_app)

    assert not apply_learned_contact_fields(contact, {})
    assert not apply_learned_contact_fields(contact, None)
    assert not apply_learned_contact_fields(None, {'customer_name': 'X'})


def test_notes_are_never_written(enrich_app):
    """R6: prose never lands in Contact.notes — curated human knowledge."""
    contact = _contact(enrich_app, notes='human-authored note')

    apply_learned_contact_fields(
        contact,
        {'customer_name': 'Fred Example', 'company': 'Vacuums R Us',
         'caller_language': 'en-US'},
        custom_extras={'department': 'support'},
    )

    assert contact.notes == 'human-authored note'
