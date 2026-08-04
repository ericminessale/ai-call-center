"""One key and one insert strategy for contacts resolved from a phone number.

Two paths create contacts for the same number — the inbound-call webhook
(``api/swml.py``) and hosted-demo pairing (``services/demo_verify``). They used
to each run their own query-then-insert against their own spelling, which fails
two ways:

  * DIVERGENT KEY — pairing stored normalized ``+digits``, the call path stored
    SignalWire's raw ``from_number``. Because the strings differ,
    ``uq_contacts_workspace_phone`` does NOT catch it: you get two rows for one
    number, silently.
  * LOST RACE — the seed commits right after pairing publishes the Redis
    binding, and that binding is what lets an inbound call through the
    verify-first gate. So the call webhook can be mid-insert for the same
    number. The loser hits the constraint for real, inside a live call.

A pre-check cannot fix the second one: the gap between SELECT and INSERT is the
bug. These tests pin the behaviour that can — canonical key on write, tolerant
lookup on read, and constraint-arbitrated create that re-reads the winner.
"""
import pytest
from flask import Flask

from app import db
from app.models import Contact, Workspace
from app.services.contact_directory import find_contact, resolve_contact
from app.tenancy import DEFAULT_WORKSPACE_ID
from app.utils.phone import normalize_phone, phone_spellings

RAW = '+1 (262) 555-0199'
CANON = '+12625550199'


@pytest.fixture()
def ws_app():
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


# --------------------------------------------------------------------------
# normalize_phone
# --------------------------------------------------------------------------

@pytest.mark.parametrize('raw', [
    '+12625550199', '12625550199', '+1 262 555 0199',
    '+1 (262) 555-0199', '1-262-555-0199',
])
def test_every_spelling_collapses_to_one_key(raw):
    assert normalize_phone(raw) == CANON


@pytest.mark.parametrize('bad', [None, '', '123', 'abc', '  '])
def test_unusable_input_is_rejected_not_guessed(bad):
    assert normalize_phone(bad) is None


def test_spellings_prefers_canonical_but_keeps_the_original():
    out = phone_spellings(RAW)
    assert out[0] == CANON
    assert RAW in out


# --------------------------------------------------------------------------
# resolve_contact
# --------------------------------------------------------------------------

def test_stores_the_canonical_spelling(ws_app):
    _app, ws_id = ws_app
    c = resolve_contact(ws_id, RAW)
    db.session.commit()
    assert c.phone == CANON, 'row must be keyed canonically, not as dialled'


def test_two_paths_two_spellings_one_row(ws_app):
    """THE divergent-key regression. Pairing normalizes, the call webhook does
    not — if they do not share a key this produces two contacts and the unique
    constraint never complains."""
    _app, ws_id = ws_app

    seeded = resolve_contact(ws_id, CANON, display_name='My phone')
    db.session.commit()
    from_call = resolve_contact(ws_id, RAW, display_name=RAW)
    db.session.commit()

    assert from_call.id == seeded.id
    assert Contact.query.filter_by(workspace_id=ws_id).count() == 1
    # The seed's name survives — the caller's display_name is only for CREATE.
    assert from_call.display_name == 'My phone'


def test_finds_a_row_written_before_normalization_existed(ws_app):
    """Legacy rows hold the raw spelling. A canonical-only lookup would miss
    them and insert a duplicate."""
    _app, ws_id = ws_app
    legacy = Contact(workspace_id=ws_id, phone=RAW, display_name=RAW)
    db.session.add(legacy)
    db.session.commit()

    assert find_contact(ws_id, RAW).id == legacy.id
    again = resolve_contact(ws_id, RAW)
    db.session.commit()
    assert again.id == legacy.id
    assert Contact.query.filter_by(workspace_id=ws_id).count() == 1


def test_losing_the_insert_race_returns_the_winner(ws_app):
    """THE race regression. Simulates the real interleaving: our SELECT misses,
    a concurrent writer commits the row, then our INSERT fires. Must return the
    winner's row rather than raising into a live call's SWML request."""
    _app, ws_id = ws_app
    import app.services.contact_directory as cd

    real_find = cd.find_contact
    state = {'first': True}

    def racing_find(workspace_id, raw_number):
        # First lookup misses (as it really would), and while we're "deciding",
        # the other path commits the row.
        if state['first']:
            state['first'] = False
            db.session.add(Contact(
                workspace_id=workspace_id, phone=CANON, display_name='winner',
            ))
            db.session.commit()
            return None
        return real_find(workspace_id, raw_number)

    cd.find_contact = racing_find
    try:
        got = resolve_contact(ws_id, RAW, display_name='loser')
    finally:
        cd.find_contact = real_find

    assert got is not None, 'lost the race and returned nothing'
    assert got.display_name == 'winner'
    assert Contact.query.filter_by(workspace_id=ws_id).count() == 1


def test_race_recovery_leaves_the_session_usable(ws_app):
    """The savepoint must not poison the caller's transaction — api/swml.py has
    an unflushed system-user insert pending and is nowhere near its commit."""
    _app, ws_id = ws_app
    import app.services.contact_directory as cd

    real_find = cd.find_contact
    state = {'first': True}

    def racing_find(workspace_id, raw_number):
        if state['first']:
            state['first'] = False
            db.session.add(Contact(
                workspace_id=workspace_id, phone=CANON, display_name='winner',
            ))
            db.session.commit()
            return None
        return real_find(workspace_id, raw_number)

    cd.find_contact = racing_find
    try:
        resolve_contact(ws_id, RAW)
    finally:
        cd.find_contact = real_find

    # Caller carries on and commits unrelated work.
    db.session.add(Workspace(name='After', status=Workspace.STATUS_ACTIVE))
    db.session.commit()
    assert Workspace.query.filter_by(name='After').first() is not None


def test_workspaces_stay_isolated(ws_app):
    _app, ws_id = ws_app
    other = Workspace(name='Other', status=Workspace.STATUS_ACTIVE)
    db.session.add(other)
    db.session.commit()

    a = resolve_contact(ws_id, RAW)
    b = resolve_contact(other.id, RAW)
    db.session.commit()

    assert a.id != b.id
    assert find_contact(ws_id, RAW).id == a.id
    assert find_contact(other.id, RAW).id == b.id


def test_unusable_number_creates_nothing(ws_app):
    _app, ws_id = ws_app
    assert resolve_contact(ws_id, '123') is None
    assert resolve_contact(ws_id, None) is None
    assert Contact.query.filter_by(workspace_id=ws_id).count() == 0
