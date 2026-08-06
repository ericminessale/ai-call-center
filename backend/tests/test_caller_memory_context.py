"""_caller_memory — the tiered contact block /internal/call-context serves
to inbound agents (R1, CONTEXT_AUDIT_2026-08-04).

Pins the disclosure tiering: identity-to-confirm and last-topic only.
Contact.notes and custom_fields must never appear in the payload (spoofable
caller ID — those stay behind a verified lookup), phone-shaped and
demo-placeholder display names are never offered as names, and the
previous-call count excludes the call being rendered.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.api.internal import _caller_memory
from app.models import Call, Contact, User, Workspace
from app.models.callback import Callback
from app.services.demo_verify import _SELF_CONTACT_NAME


@pytest.fixture()
def memory_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        ws = Workspace(name='Memory test')
        db.session.add(ws)
        db.session.flush()
        user = User(
            workspace_id=ws.id,
            email='agent@example.test',
            password_hash='not-used',
            name='Agent',
        )
        db.session.add(user)
        db.session.commit()
        yield ws, user
        db.session.remove()
        db.drop_all()


def _call(ws, user, contact=None, sid='call-current', status='ai_active',
          created_at=None, **kwargs):
    call = Call(
        workspace_id=ws.id,
        user_id=user.id,
        contact_id=contact.id if contact else None,
        signalwire_call_sid=sid,
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction=kwargs.pop('direction', 'inbound'),
        handler_type='ai',
        status=status,
        created_at=created_at or datetime.utcnow(),
        **kwargs,
    )
    db.session.add(call)
    db.session.commit()
    return call


def _contact(ws, **kwargs):
    defaults = dict(
        workspace_id=ws.id,
        phone='+15555550100',
        display_name='+15555550100',
    )
    defaults.update(kwargs)
    contact = Contact(**defaults)
    db.session.add(contact)
    db.session.commit()
    return contact


def test_no_contact_yields_empty_trio(memory_app):
    ws, user = memory_app
    call = _call(ws, user, contact=None)

    assert _caller_memory(call) == (None, None, None)


def test_unknown_caller_phone_display_is_not_a_name(memory_app):
    ws, user = memory_app
    contact = _contact(ws)  # display_name == phone
    call = _call(ws, user, contact=contact)

    block, last, cb = _caller_memory(call)

    assert block is not None
    assert block['name'] is None
    assert block['name_known'] is False
    assert block['previous_calls'] == 0
    assert last is None and cb is None


def test_demo_self_contact_placeholder_is_not_a_name(memory_app):
    ws, user = memory_app
    contact = _contact(ws, display_name=_SELF_CONTACT_NAME)
    call = _call(ws, user, contact=contact)

    block, _last, _cb = _caller_memory(call)

    assert block['name'] is None
    assert block['name_known'] is False


def test_placeholder_display_is_never_offered_even_with_a_first_name(memory_app):
    """Verification audit A-4. The demo seeds display_name='My phone', then AI
    enrichment fills first_name and leaves that display alone — so a naive
    `first_name or <guarded display>` test made name_known True while the
    emitted name was still the placeholder. The agent then asks the caller
    "Am I speaking with My phone?".
    """
    ws, user = memory_app
    cases = [
        (_SELF_CONTACT_NAME, 'Fred', 'Fred'),
        ('+15555550100', 'Robert', 'Robert'),
        ('555-555-0100', 'Dana', 'Dana'),
        ('Fred Example', 'Fred', 'Fred Example'),
    ]
    for i, (display, first, expected) in enumerate(cases):
        contact = _contact(
            ws, phone=f'+1555555{i:04d}', display_name=display, first_name=first,
        )
        call = _call(ws, user, contact=contact, sid=f'call-name-{i}')

        block, _last, _cb = _caller_memory(call)

        assert block['name'] == expected, f'display={display!r} first={first!r}'
        assert block['name_known'] is True
        assert block['name'] != _SELF_CONTACT_NAME
        assert not str(block['name']).startswith('+')


def test_no_usable_name_anywhere_reports_unknown(memory_app):
    """A phone-shaped display AND a phone-shaped first_name means we have no
    name to confirm — the greeting must fall back to asking."""
    ws, user = memory_app
    contact = _contact(
        ws, phone='+15555559999', display_name='+15555559999',
        first_name='+15555559999',
    )
    call = _call(ws, user, contact=contact, sid='call-noname')

    block, _last, _cb = _caller_memory(call)

    assert block['name'] is None
    assert block['name_known'] is False


def test_known_caller_with_history(memory_app):
    ws, user = memory_app
    contact = _contact(
        ws, display_name='Fred Example', first_name='Fred',
        account_tier='pro', is_vip=True,
    )
    prior = _call(
        ws, user, contact=contact, sid='call-prior', status='ended',
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    prior.ended_at = datetime.utcnow() - timedelta(days=2)
    prior.disposition_code = 'technical-issue'
    prior.agent_notes = 'Vacuum still losing suction after filter clean.'
    prior.ai_context_dict = {'parsed_summary': {'reason': 'vacuum loses suction'}}
    prior.caller_language = 'en-US'
    db.session.commit()
    current = _call(ws, user, contact=contact, sid='call-current-2')

    block, last, _cb = _caller_memory(current)

    assert block['name'] == 'Fred Example'
    assert block['name_known'] is True
    assert block['account_tier'] == 'pro'
    assert block['is_vip'] is True
    # Excludes the call being rendered.
    assert block['previous_calls'] == 1
    assert last is not None
    assert last['reason'] == 'vacuum loses suction'
    assert last['disposition'] == 'technical-issue'
    assert 'suction' in last['summary_short']
    assert last['caller_language'] == 'en-US'


def test_live_calls_are_not_history(memory_app):
    """F-15: a non-terminal prior call is neither countable nor offerable —
    forged/parked 'waiting' rows must not make a caller read as returning."""
    ws, user = memory_app
    contact = _contact(ws, first_name='Fred')
    _call(ws, user, contact=contact, sid='call-live', status='ai_active',
          created_at=datetime.utcnow() - timedelta(hours=1))
    current = _call(ws, user, contact=contact, sid='call-current-3')

    block, last, _cb = _caller_memory(current)

    assert block['previous_calls'] == 0
    assert last is None  # live call is not history yet


def test_summary_short_is_bounded(memory_app):
    ws, user = memory_app
    contact = _contact(ws, first_name='Fred')
    prior = _call(ws, user, contact=contact, sid='call-prior-2', status='ended')
    prior.agent_notes = 'x' * 500
    db.session.commit()
    current = _call(ws, user, contact=contact, sid='call-current-4')

    _block, last, _cb = _caller_memory(current)

    assert len(last['summary_short']) <= 280


def test_pending_callback_is_surfaced(memory_app):
    ws, user = memory_app
    contact = _contact(ws, first_name='Fred')
    cb = Callback(
        workspace_id=ws.id,
        contact_id=contact.id,
        phone_number=contact.phone,
        reason='vacuum warranty question',
        requested_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.session.add(cb)
    db.session.commit()
    current = _call(ws, user, contact=contact, sid='call-current-5')

    _block, _last, cb_block = _caller_memory(current)

    assert cb_block is not None
    assert cb_block['reason'] == 'vacuum warranty question'


def test_workspace_mismatch_returns_empty(memory_app):
    ws, user = memory_app
    other = Workspace(name='Other tenant')
    db.session.add(other)
    db.session.flush()
    foreign_contact = Contact(
        workspace_id=other.id, phone='+15555550100', display_name='Mallory',
    )
    db.session.add(foreign_contact)
    db.session.commit()
    call = _call(ws, user)
    call.contact_id = foreign_contact.id  # simulate a mis-bound row
    db.session.commit()

    assert _caller_memory(call) == (None, None, None)


def test_payload_never_contains_notes_or_custom_fields(memory_app):
    """The tiering contract itself: notes/custom_fields stay server-side."""
    ws, user = memory_app
    contact = _contact(ws, first_name='Fred', notes='SECRET human note')
    contact.custom_fields_dict = {'ssn_last4': '1234'}
    db.session.commit()
    current = _call(ws, user, contact=contact, sid='call-current-6')

    block, last, cb = _caller_memory(current)

    import json
    flat = json.dumps([block, last, cb])
    assert 'SECRET' not in flat
    assert '1234' not in flat
    assert 'notes' not in (block or {})
    assert 'custom_fields' not in (block or {})
