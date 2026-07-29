"""MAX_WORKSPACES cap enforcement in the nightly safety pass.

``nightly_safety_pass`` reaps everything beyond the cap, ordered by
``last_active_at`` descending. It used to label EVERY non-template row "live"
regardless of status or expiry, so a dead row whose reap had failed still
occupied a cap slot — pushing a genuinely active workspace over the boundary
and getting a live visitor's workspace deleted instead.

"Live" here must mean the same thing it means in ``Workspace.is_live()`` and in
``provision_workspace``'s cap count: active AND not past its TTL.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

import app.services.demo_reset as demo_reset
from app import db
from app.models import Workspace
from app.tenancy import DEFAULT_WORKSPACE_ID


@pytest.fixture()
def reset_app(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Keep the template row at DEFAULT_WORKSPACE_ID so the id != filter is
        # exercised the way it is in production.
        db.session.add(Workspace(id=DEFAULT_WORKSPACE_ID, name='Template'))
        db.session.commit()

        monkeypatch.setattr(demo_reset, 'is_demo_mode', lambda: True)
        monkeypatch.setattr(demo_reset, '_wipe_template_interactions', lambda: {})
        yield app
        db.session.remove()
        db.drop_all()


def _ws(name, *, minutes_idle, status=Workspace.STATUS_ACTIVE, expired=False):
    now = datetime.utcnow()
    ws = Workspace(
        name=name,
        status=status,
        last_active_at=now - timedelta(minutes=minutes_idle),
        expires_at=now - timedelta(hours=1) if expired else now + timedelta(days=7),
    )
    db.session.add(ws)
    return ws


def _reaped_names(monkeypatch, cap):
    """Run the cap pass with reaping/reseeding stubbed; return what it reaped."""
    reaped = []
    monkeypatch.setattr(demo_reset, 'max_workspaces', lambda: cap)
    monkeypatch.setattr(demo_reset, 'reap_expired_workspaces', lambda: {'reaped': 0})
    monkeypatch.setattr(
        demo_reset, 'reap_workspace',
        lambda ws: reaped.append(ws.name) or {'workspace': ws.name},
    )
    result = demo_reset.nightly_safety_pass()
    return reaped, result


def test_dead_rows_do_not_consume_cap_slots(reset_app, monkeypatch):
    """REGRESSION. Two undead rows + two live ones under a cap of 2 used to
    reap a live workspace; only live rows count toward the cap now."""
    _ws('expired-unreaped', minutes_idle=5, expired=True)
    _ws('already-reaped', minutes_idle=1, status=Workspace.STATUS_REAPED)
    _ws('live-recent', minutes_idle=10)
    _ws('live-older', minutes_idle=20)
    db.session.commit()

    reaped, result = _reaped_names(monkeypatch, cap=2)

    assert reaped == []
    assert result['live_workspaces'] == 2


def test_oldest_idle_live_workspace_is_reaped_over_the_cap(reset_app, monkeypatch):
    """The cap still bites — this is the behaviour being preserved."""
    _ws('live-newest', minutes_idle=1)
    _ws('live-middle', minutes_idle=30)
    _ws('live-oldest', minutes_idle=90)
    db.session.commit()

    reaped, result = _reaped_names(monkeypatch, cap=2)

    assert reaped == ['live-oldest']
    assert result['live_workspaces'] == 2


def test_template_workspace_is_never_counted_or_reaped(reset_app, monkeypatch):
    _ws('only-visitor', minutes_idle=5)
    db.session.commit()

    reaped, result = _reaped_names(monkeypatch, cap=1)

    assert reaped == []
    assert result['live_workspaces'] == 1


def test_non_expiring_workspace_counts_as_live(reset_app, monkeypatch):
    """NULL expires_at means non-expiring (matches Workspace.is_live), so it
    must occupy a slot rather than be filtered out as dead."""
    never = _ws('never-expires', minutes_idle=5)
    never.expires_at = None
    _ws('live-older', minutes_idle=60)
    db.session.commit()

    reaped, result = _reaped_names(monkeypatch, cap=1)

    assert reaped == ['live-older']
    assert result['live_workspaces'] == 1
