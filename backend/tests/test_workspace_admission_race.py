"""Same-cookie race in provision_workspace's admission path.

``provision_workspace`` calls ``resume_workspace`` FIRST, then takes the
admission lock. Anything that commits a workspace for the same cookie in that
window makes the resume result stale, so the row holding the cookie's hash is
live — not the "expired while the cookie lived on" case the hash-freeing branch
was written for. Clearing it there provisions a SECOND workspace and orphans the
first: unreachable (nothing maps to it) but still holding a cap slot until TTL.

Serializing admission made this MORE likely rather than less. Without the lock
both requests raced the unique ``session_token_hash`` INSERT and the loser
recovered through the ``IntegrityError`` handler; with it, the second request
runs after the first commits and cleanly steals the binding instead. So the
re-check has to happen under the lock.
"""
import pytest
from flask import Flask

import app.services.workspace_provision as wp
from app import db
from app.models import User, Workspace
from app.models.user import ROLE_VISITOR
from app.tenancy import DEFAULT_WORKSPACE_ID

COOKIE = 'visitor-cookie-token'


@pytest.fixture()
def provision_app(monkeypatch):
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
        db.session.commit()
        # Keep the unit hermetic: these all reach for Redis. bump_daily is
        # imported inside provision_workspace and its failure is swallowed, so
        # without the stub the create path silently burns ~24s per test on
        # connection retries.
        monkeypatch.setattr(wp, 'touch_workspace', lambda ws: None)
        monkeypatch.setattr(wp, 'bump_workspace_epoch', lambda pid: 1)
        monkeypatch.setattr(wp, 'mark_session_alive', lambda pid: None)
        monkeypatch.setattr(
            'app.services.demo_telemetry.bump_daily', lambda *a, **k: None,
        )
        yield app
        db.session.remove()
        db.drop_all()


def _committed_workspace(cookie=COOKIE):
    """A live workspace + owner already bound to this cookie — i.e. what the
    concurrent request committed while we were between resume and lock."""
    ws = Workspace(
        public_id='ws-public-1',
        name='My Call Center',
        status=Workspace.STATUS_ACTIVE,
        session_token_hash=wp._hash_token(cookie),
    )
    db.session.add(ws)
    db.session.flush()
    db.session.add(User(
        workspace_id=ws.id,
        email='owner@ws-1.demo.invalid',
        name='Demo Admin',
        role=ROLE_VISITOR,
        is_active=True,
        password_hash='x',
    ))
    db.session.commit()
    return ws


def _blind_first_resume(monkeypatch):
    """Make the pre-lock resume_workspace miss exactly once — the race window."""
    real = wp.resume_workspace
    state = {'first': True}

    def flaky(token):
        if state['first']:
            state['first'] = False
            return None
        return real(token)

    monkeypatch.setattr(wp, 'resume_workspace', flaky)


def test_live_binding_is_resumed_not_stolen(provision_app, monkeypatch):
    """REGRESSION. The stale pre-lock read must not turn a live sibling
    workspace into a second one."""
    ws = _committed_workspace()
    original_id, original_public = ws.id, ws.public_id
    _blind_first_resume(monkeypatch)

    result = wp.provision_workspace(COOKIE)

    assert result is not None
    resumed, owner = result
    assert resumed.id == original_id
    assert resumed.public_id == original_public
    assert owner.workspace_id == original_id
    # No second workspace, and the cookie still resolves to the first.
    visitors = Workspace.query.filter(Workspace.id != DEFAULT_WORKSPACE_ID).all()
    assert len(visitors) == 1
    assert visitors[0].session_token_hash == wp._hash_token(COOKIE)


def test_dead_binding_is_still_reclaimed(provision_app, monkeypatch):
    """The behaviour being preserved: a genuinely dead row's hash is freed and
    the visitor gets a fresh workspace."""
    dead = _committed_workspace()
    dead.status = Workspace.STATUS_EXPIRED
    db.session.commit()
    dead_id = dead.id

    result = wp.provision_workspace(COOKIE)

    assert result is not None
    fresh, owner = result
    assert fresh.id != dead_id
    assert fresh.session_token_hash == wp._hash_token(COOKIE)
    assert owner.workspace_id == fresh.id
    # The dead row survives (the reaper deletes it) but no longer holds the hash.
    assert db.session.get(Workspace, dead_id).session_token_hash is None


def test_ownerless_live_binding_is_reclaimed(provision_app, monkeypatch):
    """A live row with no owner user can't be resumed (nothing to mint a JWT
    for), so it must fall through to re-provisioning rather than 500."""
    orphan = _committed_workspace()
    User.query.filter_by(workspace_id=orphan.id).delete()
    db.session.commit()
    orphan_id = orphan.id
    _blind_first_resume(monkeypatch)

    result = wp.provision_workspace(COOKIE)

    assert result is not None
    fresh, _ = result
    assert fresh.id != orphan_id
    assert db.session.get(Workspace, orphan_id).session_token_hash is None
