from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.models import Call, HandlingSegment, Queue, QueueAttempt, User, Workspace
from app.services.interaction_timeline import (
    calculate_service_level,
    finish_handling_segments,
    get_agent_performance,
    get_queue_volume,
    record_human_started,
    record_queue_entered,
    record_queue_offer_declined,
    record_queue_offered,
    record_return_to_queue,
    record_status_transition,
    start_handling_segment,
)


@pytest.fixture()
def timeline_app():
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


def _interaction():
    workspace = Workspace(name='Timeline test')
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
    queue = Queue(
        workspace_id=workspace.id,
        slug='support',
        display_name='Support',
        routing_strategy='round_robin',
    )
    call = Call(
        workspace_id=workspace.id,
        user_id=agent.id,
        signalwire_call_sid='call-timeline-test',
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        ai_agent_name='Support AI',
        status='answered',
        queue_id='support',
        transport='conference',
    )
    db.session.add_all([queue, call])
    db.session.commit()
    return workspace, agent, call


def test_queue_returns_keep_sla_clock_and_split_handling(timeline_app):
    workspace, agent, call = _interaction()
    started = datetime(2026, 7, 18, 12, 0, 0)

    start_handling_segment(
        call, 'ai', ai_agent_name='Support AI', at=started,
    )
    first = record_queue_entered(
        call, 'support', priority=3, entered_at=started + timedelta(seconds=10),
    )
    record_queue_offered(call, agent.id, started + timedelta(seconds=15))
    # Webhook retries do not invent another offer.
    record_queue_offered(call, agent.id, started + timedelta(seconds=15))
    record_queue_offer_declined(call, agent.id, started + timedelta(seconds=16))
    record_queue_offered(call, agent.id, started + timedelta(seconds=20))
    record_human_started(call, agent.id, started + timedelta(seconds=25))
    finish_handling_segments(
        call, ('human',), at=started + timedelta(seconds=55),
        reason='returned_to_queue:cannot-resolve',
    )
    second = record_return_to_queue(
        call, 'support', reason='cannot-resolve', priority=3,
        at=started + timedelta(seconds=55),
    )
    record_queue_offered(call, agent.id, started + timedelta(seconds=60))
    record_human_started(call, agent.id, started + timedelta(seconds=65))
    finish_handling_segments(
        call, ('human',), at=started + timedelta(seconds=95), reason='completed',
    )
    db.session.commit()

    assert first.attempt_number == 1
    assert first.offer_count == 2
    assert first.declined_offer_count == 1
    assert first.wait_seconds == 15
    assert second.attempt_number == 2
    assert second.entered_at == started + timedelta(seconds=55)
    assert second.service_started_at == started + timedelta(seconds=10)
    assert second.wait_seconds == 55

    segments = HandlingSegment.query.order_by(HandlingSegment.started_at).all()
    assert [(row.segment_type, row.duration_seconds) for row in segments] == [
        ('ai', 10),
        ('human', 30),
        ('human', 30),
    ]

    performance = get_agent_performance(
        started - timedelta(seconds=1), workspace.id,
    )[agent.id]
    assert performance['calls_handled'] == 1
    assert performance['total_talk_time'] == 60
    assert performance['average_handle_time'] == 60.0
    assert performance['returned_to_queue'] == 1

    assert calculate_service_level(
        'support', started, 30, workspace.id,
    ) == 50.0
    assert QueueAttempt.query.count() == 2


def test_metrics_combine_timeline_and_legacy_without_double_counting(timeline_app):
    workspace, agent, timeline_call = _interaction()
    started = datetime(2026, 7, 18, 13, 0, 0)
    timeline_call.created_at = started
    timeline_call.answered_at = started
    timeline_call.ended_at = started + timedelta(seconds=200)
    attempt = record_queue_entered(
        timeline_call, 'support', entered_at=started,
    )
    record_human_started(timeline_call, agent.id, started + timedelta(seconds=5))
    finish_handling_segments(
        timeline_call, ('human',), at=started + timedelta(seconds=25),
        reason='completed',
    )

    legacy = Call(
        workspace_id=workspace.id,
        user_id=agent.id,
        assigned_agent_id=agent.id,
        signalwire_call_sid='legacy-call-test',
        destination='+15555550300',
        destination_type='phone',
        direction='inbound',
        handler_type='human',
        status='completed',
        queue_id='support',
        created_at=started,
        answered_at=started + timedelta(seconds=10),
        ended_at=started + timedelta(seconds=30),
    )
    db.session.add(legacy)
    db.session.commit()

    performance = get_agent_performance(
        started - timedelta(seconds=1), workspace.id,
    )[agent.id]
    assert performance['calls_handled'] == 2
    # Timeline call contributes its measured 20s, not its legacy 200s span.
    assert performance['total_talk_time'] == 40
    assert performance['average_handle_time'] == 20.0
    assert attempt.wait_seconds == 5
    assert calculate_service_level(
        'support', started, 6, workspace.id,
    ) == 50.0
    assert get_queue_volume('support', started, workspace.id) == {
        'offered': 2,
        'answered': 2,
        'abandoned': 0,
    }


def test_hold_resume_does_not_invent_a_second_answered_attempt(timeline_app):
    """A hold/resume cycle is one queue journey, not two.

    Returning to 'active' calls record_human_started, whose record_queue_accepted
    used to find no OPEN attempt (accept closes it) and mint a fresh one that it
    then immediately stamped accepted/'answered' — so a single accepted call
    reported two answered attempts and doubled every derived queue metric.
    """
    _workspace, agent, call = _interaction()
    started = datetime(2026, 7, 20, 9, 0, 0)

    accepted = record_queue_entered(call, 'support', entered_at=started)
    call.assigned_agent_id = agent.id
    record_human_started(call, agent.id, started + timedelta(seconds=10))
    db.session.commit()

    assert QueueAttempt.query.count() == 1

    record_status_transition(call, 'active', 'on_hold')
    record_status_transition(call, 'on_hold', 'active')
    db.session.commit()

    attempts = QueueAttempt.query.all()
    assert len(attempts) == 1
    assert attempts[0].id == accepted.id
    assert attempts[0].attempt_number == 1
    assert [row.exit_reason for row in attempts] == ['answered']

    # The handling record still shows the hold: human, hold, then human again.
    segments = HandlingSegment.query.order_by(HandlingSegment.started_at).all()
    assert [row.segment_type for row in segments] == [
        'human', 'hold', 'human',
    ]

    # A REAL re-queue still opens attempt 2 — the guard only suppresses the
    # implicit re-accept, never an explicit return-to-queue.
    record_return_to_queue(call, 'support', reason='cannot-resolve')
    record_human_started(call, agent.id)
    db.session.commit()
    assert QueueAttempt.query.count() == 2


def test_status_hook_records_ai_and_keeps_call_control_resilient(
    timeline_app, monkeypatch,
):
    _workspace, _agent, call = _interaction()
    call.status = 'ringing'
    call.answered_at = None
    db.session.commit()

    call.update_status('answered')
    db.session.commit()
    ai_segment = HandlingSegment.query.filter_by(
        call_id=call.id, segment_type='ai', ended_at=None,
    ).one()
    assert ai_segment.ai_agent_name == 'Support AI'

    import app.services.interaction_timeline as timeline

    def fail_recorder(*_args, **_kwargs):
        raise RuntimeError('simulated analytics outage')

    monkeypatch.setattr(timeline, 'record_status_transition', fail_recorder)
    call.update_status('ended')
    db.session.commit()
    assert call.status == 'ended'
    assert call.ended_at is not None
