"""finalize_call_memory — the centralized caller-memory finalizer
(F-02/F-04/F-05, CONTEXT_MEMORY_VERIFICATION_AUDIT 2026-08-04).

Pins the two reproductions from the verification audit:
- F-02: a Call mis-bound to a foreign workspace's Contact must never write
  that contact's stats or digest (the auditor reproduced exactly this
  contamination against the pre-fix inline writers).
- F-04: finalization is a no-op while the call is live and fully effective
  once the SAME handler later flips it terminal — the AI-only self-ended
  ordering that previously left the digest empty forever.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.models import Call, Contact, User, Workspace
from app.services.contact_enrichment import finalize_call_memory


@pytest.fixture()
def final_app(monkeypatch):
    # The index push is best-effort HTTP — stub it so tests stay hermetic
    # and we can assert it only fires on legitimate finalizations.
    pushed = []
    import app.services.interaction_index as idx

    monkeypatch.setattr(
        idx, 'index_call_summary',
        lambda call, entry: pushed.append((call.id, entry)) or True,
    )

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        ws_a = Workspace(name='Workspace A')
        ws_b = Workspace(name='Workspace B')
        db.session.add_all([ws_a, ws_b])
        db.session.flush()
        user_a = User(workspace_id=ws_a.id, email='a@example.test',
                      password_hash='x', name='A')
        user_b = User(workspace_id=ws_b.id, email='b@example.test',
                      password_hash='x', name='B')
        contact_a = Contact(workspace_id=ws_a.id, phone='+15555550100',
                            display_name='Fred A', first_name='Fred')
        db.session.add_all([user_a, user_b, contact_a])
        db.session.commit()
        yield ws_a, ws_b, user_a, user_b, contact_a, pushed
        db.session.remove()
        db.drop_all()


def _call(ws, user, contact_id, sid, status='ended', summary=None):
    call = Call(
        workspace_id=ws.id,
        user_id=user.id,
        contact_id=contact_id,
        signalwire_call_sid=sid,
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        status=status,
        created_at=datetime.utcnow() - timedelta(minutes=10),
    )
    call.ended_at = datetime.utcnow() if status in Call.TERMINAL_STATUSES else None
    call.summary = summary
    db.session.add(call)
    db.session.commit()
    return call


def test_cross_workspace_call_cannot_write_foreign_digest(final_app):
    """The auditor's F-02 reproduction, now refused."""
    ws_a, ws_b, _ua, user_b, contact_a, pushed = final_app
    poison = _call(ws_b, user_b, contact_a.id, 'call-poison',
                   summary='workspace-B poison')

    result = finalize_call_memory(poison)

    assert result is None
    db.session.refresh(contact_a)
    assert contact_a.interaction_digest is None
    assert contact_a.total_calls == 0
    assert pushed == []


def test_foreign_calls_excluded_from_digest_even_if_bound(final_app):
    """Belt: a pre-existing mis-bound row never leaks into the digest built
    by a legitimate same-workspace finalization."""
    ws_a, ws_b, user_a, user_b, contact_a, _pushed = final_app
    _call(ws_b, user_b, contact_a.id, 'call-poison', summary='workspace-B poison')
    good = _call(ws_a, user_a, contact_a.id, 'call-good', summary='legit summary')

    finalize_call_memory(good)

    db.session.refresh(contact_a)
    digest = contact_a.interaction_digest_list
    assert len(digest) == 1
    assert digest[0]['summary'] == 'legit summary'
    assert 'poison' not in (contact_a.interaction_digest or '')
    # The digest filters by workspace; Contact.update_stats() (called by the
    # same finalizer) walks the bare relationship and does NOT — so a
    # mis-bound foreign call would inflate these two. Pinning them keeps the
    # digest and the counters telling the same story (verification audit B-1).
    assert contact_a.total_calls == 1
    assert contact_a.last_interaction_at == good.created_at


def test_noop_while_live_effective_once_terminal(final_app):
    """The F-04 ordering: same call, finalize before AND after the terminal
    transition — the second pass must land everything."""
    ws_a, _wsb, user_a, _ub, contact_a, pushed = final_app
    call = _call(ws_a, user_a, contact_a.id, 'call-selfend',
                 status='ai_active', summary='ai handled it')

    assert finalize_call_memory(call) is None  # live → no-op
    db.session.refresh(contact_a)
    assert contact_a.interaction_digest is None
    assert pushed == []

    call.status = 'ended'
    call.ended_at = datetime.utcnow()
    db.session.commit()

    result = finalize_call_memory(call)

    assert result is not None
    db.session.refresh(contact_a)
    digest = contact_a.interaction_digest_list
    assert len(digest) == 1
    assert digest[0]['summary'] == 'ai handled it'
    assert contact_a.total_calls == 1
    assert [cid for cid, _ in pushed] == [call.id]


def test_finalize_is_idempotent_across_paths(final_app):
    """F-05: every terminal path calls this — repeated invocations (webhook
    + conference end + watchdog) must converge, not accumulate."""
    ws_a, _wsb, user_a, _ub, contact_a, pushed = final_app
    call = _call(ws_a, user_a, contact_a.id, 'call-multi', summary='done')

    for _ in range(3):
        assert finalize_call_memory(call) is not None

    db.session.refresh(contact_a)
    assert contact_a.total_calls == 1
    assert len(contact_a.interaction_digest_list) == 1
    # Index push repeats are fine — the endpoint upserts by call_id (F-08).
    assert all(cid == call.id for cid, _ in pushed)
