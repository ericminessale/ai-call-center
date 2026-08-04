"""Pairing seeds the visitor's own number as a contact — exactly once.

Contacts are otherwise born on the first inbound call (the look-up-or-create on
``from_number`` in ``api/swml.py``), leaving the Contacts screen empty for the
whole window between verifying and calling.

The risk this file guards is DUPLICATION, not creation: two code paths now
insert a contact for the same phone. They must agree on the key
``(workspace_id, phone)`` with phone in ``_norm``-ed E.164, or a visitor ends up
with two rows for one number — and ``uq_contacts_workspace_phone`` turns the
second one into an IntegrityError that would abort whatever transaction it lands
in.
"""
import pytest
from flask import Flask

import app.services.demo_verify as dv
from app import db
from app.models import Contact, Workspace
from app.tenancy import DEFAULT_WORKSPACE_ID

NUMBER = '+12625550142'


@pytest.fixture()
def verify_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(Workspace(id=DEFAULT_WORKSPACE_ID, name='Template'))
        ws = Workspace(name='Visitor', status=Workspace.STATUS_ACTIVE)
        db.session.add(ws)
        db.session.commit()
        yield app, ws.id
        db.session.remove()
        db.drop_all()


def _contacts(ws_id, phone=NUMBER):
    return Contact.query.filter_by(workspace_id=ws_id, phone=phone).all()


def test_seeds_one_contact_for_the_verified_number(verify_app):
    _app, ws_id = verify_app

    cid = dv._ensure_self_contact(ws_id, NUMBER)

    rows = _contacts(ws_id)
    assert len(rows) == 1
    assert rows[0].id == cid
    assert rows[0].phone == NUMBER
    # 'prospect' tier renders no tier chip on the row — the visitor's own
    # phone shouldn't wear a customer badge.
    assert rows[0].account_tier == 'prospect'
    assert rows[0].display_name == dv._SELF_CONTACT_NAME


def test_is_idempotent_across_repeated_pairings(verify_app):
    """Re-pairing the same number (or a retried webhook) must not stack rows."""
    _app, ws_id = verify_app

    first = dv._ensure_self_contact(ws_id, NUMBER)
    second = dv._ensure_self_contact(ws_id, NUMBER)
    third = dv._ensure_self_contact(ws_id, NUMBER)

    assert first == second == third
    assert len(_contacts(ws_id)) == 1


def test_inbound_call_lookup_finds_the_seeded_row(verify_app):
    """THE point of the shared key. Mirrors api/swml.py's lookup — if that
    query misses, the call webhook inserts a duplicate and trips the unique
    constraint."""
    _app, ws_id = verify_app
    seeded = dv._ensure_self_contact(ws_id, NUMBER)

    # Exactly what swml.py does: filter_by(phone=from_number) + workspace.
    found = (
        Contact.query.filter_by(phone=NUMBER).filter_by(workspace_id=ws_id).first()
    )

    assert found is not None, 'inbound lookup missed the seeded contact'
    assert found.id == seeded


def test_does_not_leak_across_workspaces(verify_app):
    """Same number in two workspaces is legal (the unique constraint is
    per-workspace) and must produce two independent rows, not a shared one."""
    _app, ws_id = verify_app
    other = Workspace(name='Other', status=Workspace.STATUS_ACTIVE)
    db.session.add(other)
    db.session.commit()

    a = dv._ensure_self_contact(ws_id, NUMBER)
    b = dv._ensure_self_contact(other.id, NUMBER)

    assert a != b
    assert len(_contacts(ws_id)) == 1
    assert len(_contacts(other.id)) == 1


def test_self_contact_id_is_read_only(verify_app):
    """``_self_contact_id`` backs verify_status, which is a GET — it must never
    create the row as a side effect of being asked about it."""
    _app, ws_id = verify_app

    assert dv._self_contact_id(ws_id, NUMBER) is None
    assert _contacts(ws_id) == []

    seeded = dv._ensure_self_contact(ws_id, NUMBER)
    assert dv._self_contact_id(ws_id, NUMBER) == seeded


def test_self_contact_id_tolerates_missing_inputs(verify_app):
    _app, ws_id = verify_app
    assert dv._self_contact_id(None, NUMBER) is None
    assert dv._self_contact_id(ws_id, None) is None
    assert dv._self_contact_id(ws_id, '') is None
