"""End-of-call classification for AI-handled inbound calls.

Inbound relay_script calls never get a call-state webhook (the `set` verb's
call_state_url is a plain script variable — see call_watchdog's docstring),
so a call the AI handled end-to-end used to reach its terminal transition
with answered_at=NULL and duration=0, and compute_end_reason's "never
carried audio" branch stamped every clean AI-only conversation
'abandoned_in_queue'. These tests pin the corrected contract:

  - 'ai_active' counts as answered (the SWML ran `answer`, the AI is
    conversing), so AI-only calls get real durations and classify as
    'completed';
  - a call that dies while parked 'waiting' for a human still classifies
    'abandoned_in_queue', however long the AI talked to the caller first
    (update_status hands compute_end_reason the pre-terminal status);
  - a pre-stamped end_reason (the hold timeout's 'callback_scheduled') is
    never overwritten.

Also covers the /route PGI guard: a transfer landing on a call the backend
already knows is over gets hangup SWML back and is NOT re-parked in the
queue.
"""

from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.models import Call, User, Workspace


@pytest.fixture()
def app():
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


def _make_call(**overrides):
    workspace = Workspace(name=f'ws-{datetime.utcnow().timestamp()}')
    db.session.add(workspace)
    db.session.flush()
    user = User(
        email=f'agent-{datetime.utcnow().timestamp()}@test.local',
        workspace_id=workspace.id,
    )
    user.set_password('irrelevant')
    db.session.add(user)
    db.session.flush()
    fields = dict(
        signalwire_call_sid=f"call-{datetime.utcnow().timestamp()}",
        user_id=user.id,
        workspace_id=workspace.id,
        destination='+15550001111',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        status='initiated',
    )
    fields.update(overrides)
    call = Call(**fields)
    db.session.add(call)
    db.session.flush()
    return call


class TestAnsweredAtStamping:
    def test_ai_active_stamps_answered_at(self, app):
        call = _make_call()
        assert call.answered_at is None
        call.update_status('ai_active')
        assert call.answered_at is not None

    def test_ai_active_does_not_restamp(self, app):
        call = _make_call()
        call.update_status('ai_active')
        first = call.answered_at
        call.update_status('ai_active')
        assert call.answered_at == first


class TestEndReasonClassification:
    def test_clean_ai_only_call_is_completed_not_abandoned(self, app):
        """The fred_returning_caller call-1 shape: AI answers, converses for
        minutes, caller says goodbye and hangs up, post-prompt closes the
        call. Used to classify 'abandoned_in_queue'."""
        call = _make_call()
        call.update_status('ai_active')
        # Backdate the answer so the sealed duration is a normal call length.
        call.answered_at = datetime.utcnow() - timedelta(seconds=150)
        call.update_status('ended')
        assert call.end_reason == 'completed'
        assert call.duration and call.duration >= 149

    def test_death_while_waiting_is_still_abandoned(self, app):
        """AI triage answered the call, transferred to a human queue, caller
        gave up on hold. answered_at being set must not flip this to
        'completed' — the queue is where we lost them."""
        call = _make_call()
        call.update_status('ai_active')
        call.answered_at = datetime.utcnow() - timedelta(seconds=200)
        call.status = 'waiting'
        call.update_status('ended')
        assert call.end_reason == 'abandoned_in_queue'

    def test_death_while_assigned_is_missed(self, app):
        call = _make_call()
        call.update_status('ai_active')
        call.assigned_agent_id = call.user_id
        call.status = 'assigned'
        call.update_status('ended')
        assert call.end_reason == 'missed'

    def test_prestamped_end_reason_wins(self, app):
        """The hold timeout stamps 'callback_scheduled' before ending the
        call; the terminal transition must keep it."""
        call = _make_call()
        call.update_status('ai_active')
        call.end_reason = 'callback_scheduled'
        call.status = 'waiting'
        call.update_status('ended')
        assert call.end_reason == 'callback_scheduled'

    def test_watchdog_precomputed_shape_unchanged(self, app):
        """reap_call computes BEFORE flipping status (self.status is still the
        pre-end status there) — the no-argument call must keep working and
        classify a parked call as abandoned."""
        call = _make_call(status='waiting')
        assert call.compute_end_reason() == 'abandoned_in_queue'


class TestRouteRefusesTerminalCalls:
    @pytest.fixture()
    def client(self, app, monkeypatch):
        monkeypatch.setenv('WEBHOOK_AUTH_REQUIRED', 'false')
        monkeypatch.delenv('DEMO_MODE', raising=False)
        monkeypatch.delenv('TENANCY_MODE', raising=False)
        from app.api.queues import queues_bp
        app.register_blueprint(queues_bp, url_prefix='/api/queues')
        return app.test_client()

    def test_terminal_call_gets_hangup_swml(self, app, client):
        call = _make_call(status='ended')
        call.ended_at = datetime.utcnow()
        call.end_reason = 'completed'
        db.session.commit()

        resp = client.post(
            f'/api/queues/sales/route',
            json={'call': {'call_id': call.signalwire_call_sid,
                           'from_number': '+15550002222'}},
        )
        assert resp.status_code == 200
        swml = resp.get_json()
        assert swml['sections']['main'] == ['hangup']
        # And the call was not re-parked.
        db.session.refresh(call)
        assert call.status == 'ended'
        assert call.queue_id is None
