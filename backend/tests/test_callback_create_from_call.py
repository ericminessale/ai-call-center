"""Callback.create_from_call caller-name snapshot chain.

The name fallback is: AI-captured ``customer_name`` → AI-captured
``caller_name`` → linked contact → None. The contact leg used to read
``call.contact.name`` — an attribute Contact does not have (it has
first_name/last_name/display_name and the ``computed_display_name``
property). So the exact case the fallback exists for — the AI captured
context but never got a name, and the caller IS a known contact — raised
AttributeError, which ``POST /api/callbacks`` surfaced as a 500. These
tests pin every leg of the chain, most importantly that one.
"""
import pytest
from flask import Flask

from app import db
from app.models import Call, Contact, User, Workspace
from app.models.callback import Callback


@pytest.fixture()
def callback_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _make_call(ai_context=None, contact=None, **contact_kwargs):
    """Build a committed inbound Call, optionally linked to a Contact."""
    workspace = Workspace(name='Callback test')
    db.session.add(workspace)
    db.session.flush()
    agent = User(
        workspace_id=workspace.id,
        email='agent@example.test',
        password_hash='not-used',
        name='Agent One',
    )
    db.session.add(agent)
    db.session.flush()

    if contact is None and contact_kwargs:
        contact = Contact(workspace_id=workspace.id, **contact_kwargs)
    if contact is not None:
        db.session.add(contact)
        db.session.flush()

    call = Call(
        workspace_id=workspace.id,
        user_id=agent.id,
        contact_id=contact.id if contact else None,
        signalwire_call_sid='call-callback-test',
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        status='answered',
        queue_id='support',
    )
    if ai_context is not None:
        call.ai_context_dict = ai_context
    db.session.add(call)
    db.session.commit()
    return call


def test_ctx_without_name_falls_back_to_contact_display_name(callback_app):
    """THE regression: context captured but nameless + linked contact must
    snapshot the contact's computed display name, not raise AttributeError."""
    call = _make_call(
        ai_context={'reason': 'billing dispute'},
        phone='+15555550100',
        first_name='Ada',
        last_name='Lovelace',
    )

    cb = Callback.create_from_call(call, queue_id='support')

    assert cb.caller_name == 'Ada Lovelace'
    assert cb.reason == 'billing dispute'
    # The row must also survive the flush path create_callback drives.
    db.session.add(cb)
    db.session.commit()
    assert cb.status == 'pending'
    assert cb.contact_id == call.contact_id


def test_bare_contact_still_yields_a_name_not_an_error(callback_app):
    """A contact with no name fields at all resolves to its phone — the
    fallback leg must never raise regardless of how sparse the contact is."""
    call = _make_call(ai_context={'issue': 'no dial tone'}, phone='+15555550100')

    cb = Callback.create_from_call(call)

    assert cb.caller_name == '+15555550100'
    assert cb.reason == 'no dial tone'


def test_ai_captured_name_beats_contact(callback_app):
    call = _make_call(
        ai_context={'customer_name': 'Grace (as given on call)'},
        phone='+15555550100',
        first_name='Grace',
        last_name='Hopper',
    )

    cb = Callback.create_from_call(call)

    assert cb.caller_name == 'Grace (as given on call)'


def test_ctx_caller_name_slot_is_second_choice(callback_app):
    call = _make_call(
        ai_context={'caller_name': 'Katherine'},
        phone='+15555550100',
        display_name='K. Johnson',
    )

    cb = Callback.create_from_call(call)

    assert cb.caller_name == 'Katherine'


def test_no_ctx_and_no_contact_leaves_name_empty(callback_app):
    call = _make_call()

    cb = Callback.create_from_call(call)

    assert cb.caller_name is None
    assert cb.phone_number == '+15555550100'
