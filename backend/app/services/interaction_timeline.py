"""Additive, transaction-local event history for contact-center analytics.

These helpers never commit. Normal callers keep their existing transaction
boundary; live-media paths can use :func:`best_effort` to isolate analytics in
a savepoint when call continuity matters more than telemetry.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func

from app import db
from app.models.handling_segment import HandlingSegment
from app.models.queue import Queue
from app.models.queue_attempt import QueueAttempt


logger = logging.getLogger(__name__)


def best_effort(recorder, *args, **kwargs):
    """Run a recorder in a savepoint so analytics never block live media."""
    try:
        with db.session.begin_nested():
            return recorder(*args, **kwargs)
    except Exception as exc:
        logger.warning(
            "Timeline recorder %s failed: %s",
            getattr(recorder, '__name__', str(recorder)), exc,
        )
        return None


def _now(value=None):
    return value or datetime.utcnow()


def _pending(model, call_id):
    return [
        row for row in db.session.new
        if isinstance(row, model) and row.call_id == call_id
    ]


def _open_attempt(call):
    if not call.id:
        return None
    for attempt in reversed(_pending(QueueAttempt, call.id)):
        if attempt.exited_at is None:
            return attempt
    with db.session.no_autoflush:
        candidates = (
            QueueAttempt.query
            .filter_by(call_id=call.id, exited_at=None)
            .order_by(QueueAttempt.attempt_number.desc())
            .all()
        )
    # A dirty identity-map row may still match its pre-update database value
    # while autoflush is suppressed. Recheck the live Python value.
    return next((row for row in candidates if row.exited_at is None), None)


def _active_segments(call, types=None, agent_id=None):
    if not call.id:
        return []
    wanted = set(types) if types else None
    pending = [
        row for row in _pending(HandlingSegment, call.id)
        if row.ended_at is None
        and (wanted is None or row.segment_type in wanted)
        and (agent_id is None or row.agent_id == agent_id)
    ]
    with db.session.no_autoflush:
        query = HandlingSegment.query.filter_by(call_id=call.id, ended_at=None)
        if wanted:
            query = query.filter(HandlingSegment.segment_type.in_(wanted))
        if agent_id is not None:
            query = query.filter_by(agent_id=agent_id)
        persisted = [
            row for row in query.order_by(HandlingSegment.started_at.asc()).all()
            if row.ended_at is None
        ]
    seen = {id(row) for row in pending}
    return pending + [row for row in persisted if id(row) not in seen]


def record_queue_entered(
    call,
    queue_slug,
    priority=None,
    entered_at=None,
    routing_strategy=None,
):
    """Open an idempotent queue stay and stop active primary handling."""
    if not call.id or not call.workspace_id or not queue_slug:
        return None

    entered_at = _now(entered_at)
    existing = _open_attempt(call)
    if existing and existing.queue_slug == queue_slug:
        return existing
    if existing:
        existing.exited_at = entered_at
        existing.exit_reason = existing.exit_reason or 'transferred'

    with db.session.no_autoflush:
        queue = Queue.query.filter_by(
            workspace_id=call.workspace_id, slug=queue_slug,
        ).first()
        max_attempt = (
            db.session.query(func.max(QueueAttempt.attempt_number))
            .filter(QueueAttempt.call_id == call.id)
            .scalar()
        ) or 0
        first_attempt = (
            QueueAttempt.query.filter_by(call_id=call.id)
            .order_by(QueueAttempt.attempt_number.asc())
            .first()
        )

    pending_numbers = [row.attempt_number for row in _pending(QueueAttempt, call.id)]
    if pending_numbers:
        max_attempt = max(max_attempt, *pending_numbers)
    service_started_at = (
        first_attempt.service_started_at if first_attempt
        else min(
            (row.service_started_at for row in _pending(QueueAttempt, call.id)),
            default=entered_at,
        )
    )
    attempt = QueueAttempt(
        workspace_id=call.workspace_id,
        call_id=call.id,
        queue_id=queue.id if queue else None,
        queue_slug=queue_slug,
        attempt_number=max_attempt + 1,
        priority=priority,
        routing_strategy=routing_strategy or (queue.routing_strategy if queue else None),
        transport=call.transport,
        entered_at=entered_at,
        service_started_at=service_started_at,
    )
    db.session.add(attempt)
    finish_handling_segments(
        call, HandlingSegment.PRIMARY_TYPES, at=entered_at,
        reason='queued_for_human',
    )
    return attempt


def _latest_attempt(call):
    """Most recent attempt for the call, open or closed (pending rows first)."""
    if not call.id:
        return None
    pending = _pending(QueueAttempt, call.id)
    if pending:
        return max(pending, key=lambda row: row.attempt_number or 0)
    with db.session.no_autoflush:
        return (
            QueueAttempt.query
            .filter_by(call_id=call.id)
            .order_by(QueueAttempt.attempt_number.desc())
            .first()
        )


def _ensure_open_attempt(call, at=None):
    attempt = _open_attempt(call)
    if attempt:
        return attempt
    if not call.queue_id:
        return None
    # An already-ANSWERED latest attempt means this call's queue stay is over
    # and nothing has re-queued it — a second "human started" for the same
    # stay (resume-from-hold, a repeated conference-join webhook, a status
    # write that re-asserts 'active') must reuse it, not open a new one.
    # Without this the fresh attempt got accepted_at/exit_reason='answered'
    # immediately, so one accepted call reported two answered attempts and
    # double-counted every queue metric derived from them.
    #
    # A genuine re-queue goes through record_return_to_queue →
    # record_queue_entered, which leaves an OPEN attempt that _open_attempt
    # above already returns, so this never suppresses a real second stay.
    latest = _latest_attempt(call)
    if latest is not None and latest.accepted_at is not None:
        return latest
    return record_queue_entered(call, call.queue_id, entered_at=at)


def record_queue_offered(call, agent_id, at=None):
    attempt = _ensure_open_attempt(call, at)
    if not attempt or agent_id is None:
        return attempt
    at = _now(at)
    # Repeated delivery of the same assignment is not another offer. A new
    # offer to that agent after a decline is counted because decline is newer.
    same_live_offer = (
        attempt.last_offered_agent_id == agent_id
        and attempt.last_offered_at is not None
        and (
            attempt.last_declined_at is None
            or attempt.last_declined_at < attempt.last_offered_at
        )
    )
    if same_live_offer:
        return attempt
    attempt.first_offered_at = attempt.first_offered_at or at
    attempt.last_offered_at = at
    attempt.last_offered_agent_id = agent_id
    attempt.offer_count = (attempt.offer_count or 0) + 1
    return attempt


def record_queue_offer_declined(call, agent_id, at=None):
    attempt = _open_attempt(call)
    if not attempt:
        return None
    at = _now(at)
    if (
        attempt.last_declined_agent_id == agent_id
        and attempt.last_declined_at
        and attempt.last_offered_at
        and attempt.last_declined_at >= attempt.last_offered_at
    ):
        return attempt
    attempt.last_declined_at = at
    attempt.last_declined_agent_id = agent_id
    attempt.declined_offer_count = (attempt.declined_offer_count or 0) + 1
    return attempt


def record_queue_accepted(call, agent_id=None, at=None):
    at = _now(at)
    attempt = _ensure_open_attempt(call, at)
    if not attempt:
        return None
    if attempt.accepted_at:
        if attempt.accepted_agent_id is None and agent_id is not None:
            attempt.accepted_agent_id = agent_id
        return attempt
    if agent_id is not None and not attempt.first_offered_at:
        record_queue_offered(call, agent_id, at)
    attempt.accepted_at = at
    attempt.accepted_agent_id = agent_id
    attempt.exited_at = at
    attempt.exit_reason = 'answered'
    return attempt


def close_open_queue_attempt(call, reason, at=None):
    attempt = _open_attempt(call)
    if not attempt:
        return None
    attempt.exited_at = _now(at)
    attempt.exit_reason = attempt.exit_reason or reason
    return attempt


def finish_handling_segments(call, types=None, agent_id=None, at=None, reason=None):
    at = _now(at)
    finished = []
    for segment in _active_segments(call, types=types, agent_id=agent_id):
        segment.ended_at = at
        segment.end_reason = segment.end_reason or reason
        finished.append(segment)
    return finished


def start_handling_segment(
    call,
    segment_type,
    agent_id=None,
    ai_agent_name=None,
    at=None,
    queue_attempt=None,
    details=None,
):
    if not call.id or not call.workspace_id or segment_type not in HandlingSegment.TYPES:
        return None
    at = _now(at)
    for segment in _active_segments(call, types=(segment_type,)):
        if segment.agent_id == agent_id and segment.ai_agent_name == ai_agent_name:
            return segment
    if segment_type in HandlingSegment.PRIMARY_TYPES:
        finish_handling_segments(
            call, HandlingSegment.PRIMARY_TYPES, at=at,
            reason=f'transition_to_{segment_type}',
        )
    segment = HandlingSegment(
        workspace_id=call.workspace_id,
        call_id=call.id,
        queue_attempt=queue_attempt,
        segment_type=segment_type,
        agent_id=agent_id,
        ai_agent_name=ai_agent_name,
        transport=call.transport,
        started_at=at,
        details=details,
    )
    db.session.add(segment)
    return segment


def record_human_started(call, agent_id=None, at=None):
    at = _now(at)
    attempt = record_queue_accepted(call, agent_id, at)
    return start_handling_segment(
        call, 'human', agent_id=agent_id, at=at, queue_attempt=attempt,
    )


def record_return_to_queue(call, queue_slug, reason=None, priority=None, at=None):
    at = _now(at)
    finish_handling_segments(
        call, HandlingSegment.PRIMARY_TYPES, at=at,
        reason=f'returned_to_queue:{reason or "unspecified"}',
    )
    return record_queue_entered(
        call, queue_slug, priority=priority, entered_at=at,
    )


def record_status_transition(call, previous_status, status):
    """Translate durable Call status changes into measured timeline events."""
    if not call.id or previous_status == status:
        return
    at = datetime.utcnow()
    if status == 'answered' and call.handler_type == 'ai':
        start_handling_segment(
            call, 'ai', ai_agent_name=call.ai_agent_name, at=call.answered_at or at,
        )
    elif status == 'ai_active' and call.handler_type == 'ai' and call.answered_at:
        start_handling_segment(
            call, 'ai', ai_agent_name=call.ai_agent_name, at=call.answered_at,
        )
    elif status in ('active', 'connected'):
        record_human_started(call, call.assigned_agent_id, at)
    elif status == 'on_hold':
        start_handling_segment(call, 'hold', agent_id=call.assigned_agent_id, at=at)

    if status in call.TERMINAL_STATUSES:
        finish_handling_segments(
            call, at=call.ended_at or at, reason=call.end_reason or status,
        )
        close_open_queue_attempt(
            call, call.end_reason or ('failed' if status == 'failed' else 'abandoned'),
            at=call.ended_at or at,
        )


def get_agent_performance(since, workspace_id=None):
    """Return human handling metrics, with a non-overlapping legacy fallback.

    Timeline rows are authoritative per call. A legacy ``Call`` contributes
    only when it has no human segment at all, preventing double-counting
    during the additive rollout.
    """
    from app.models.call import Call

    segment_query = (
        db.session.query(HandlingSegment, Call)
        .join(Call, Call.id == HandlingSegment.call_id)
        .filter(
            HandlingSegment.segment_type == 'human',
            HandlingSegment.agent_id.isnot(None),
            HandlingSegment.ended_at.isnot(None),
            HandlingSegment.ended_at >= since,
        )
    )
    if workspace_id is not None:
        segment_query = segment_query.filter(HandlingSegment.workspace_id == workspace_id)

    results = {}

    def bucket(agent_id):
        return results.setdefault(agent_id, {
            'call_ids': set(),
            'talk_seconds': 0,
            'returned_to_queue': 0,
            'sentiments': {},
        })

    for segment, call in segment_query.all():
        item = bucket(segment.agent_id)
        item['call_ids'].add(call.id)
        item['talk_seconds'] += segment.duration_seconds or 0
        if (segment.end_reason or '').startswith('returned_to_queue:'):
            item['returned_to_queue'] += 1
        if call.sentiment_score is not None:
            item['sentiments'][call.id] = float(call.sentiment_score)

    has_human_segment = db.session.query(HandlingSegment.id).filter(
        HandlingSegment.call_id == Call.id,
        HandlingSegment.segment_type == 'human',
    ).exists()
    legacy_query = Call.query.filter(
        Call.ended_at >= since,
        Call.answered_at.isnot(None),
        Call.ended_at.isnot(None),
        ~has_human_segment,
    )
    if workspace_id is not None:
        legacy_query = legacy_query.filter(Call.workspace_id == workspace_id)
    for call in legacy_query.all():
        agent_id = call.assigned_agent_id
        if agent_id is None and call.handler_type == 'human':
            agent_id = call.user_id
        if agent_id is None:
            continue
        item = bucket(agent_id)
        item['call_ids'].add(call.id)
        item['talk_seconds'] += max(
            0, int((call.ended_at - call.answered_at).total_seconds()),
        )
        item['returned_to_queue'] += call.return_count or 0
        if call.sentiment_score is not None:
            item['sentiments'][call.id] = float(call.sentiment_score)

    summarized = {}
    for agent_id, item in results.items():
        handled = len(item['call_ids'])
        sentiment_values = list(item['sentiments'].values())
        summarized[agent_id] = {
            'calls_handled': handled,
            'average_handle_time': (
                round(item['talk_seconds'] / handled, 1) if handled else 0.0
            ),
            'total_talk_time': item['talk_seconds'],
            'average_sentiment': (
                round(sum(sentiment_values) / len(sentiment_values), 2)
                if sentiment_values else None
            ),
            'returned_to_queue': item['returned_to_queue'],
        }
    return summarized


def calculate_service_level(
    queue_slug,
    since,
    threshold_seconds,
    workspace_id=None,
):
    """Calculate answer SLA from queue stays plus non-overlapping legacy calls."""
    from app.models.call import Call

    attempt_query = QueueAttempt.query.filter(
        QueueAttempt.queue_slug == queue_slug,
        QueueAttempt.accepted_at.isnot(None),
        QueueAttempt.accepted_at >= since,
    )
    if workspace_id is not None:
        attempt_query = attempt_query.filter(QueueAttempt.workspace_id == workspace_id)
    waits = [
        attempt.wait_seconds for attempt in attempt_query.all()
        if attempt.wait_seconds is not None
    ]

    has_attempt = db.session.query(QueueAttempt.id).filter(
        QueueAttempt.call_id == Call.id,
    ).exists()
    legacy_query = Call.query.filter(
        Call.queue_id == queue_slug,
        Call.answered_at.isnot(None),
        Call.answered_at >= since,
        ~has_attempt,
    )
    if workspace_id is not None:
        legacy_query = legacy_query.filter(Call.workspace_id == workspace_id)
    waits.extend(
        max(0, int((call.answered_at - call.created_at).total_seconds()))
        for call in legacy_query.all()
        if call.created_at
    )
    if not waits:
        return None
    within = sum(1 for seconds in waits if seconds <= threshold_seconds)
    return round(100.0 * within / len(waits), 1)


def get_queue_volume(queue_slug, since, workspace_id=None):
    """Return offered/answered/abandoned counts without rollout duplicates."""
    from app.models.call import Call

    attempt_query = QueueAttempt.query.filter(
        QueueAttempt.queue_slug == queue_slug,
        QueueAttempt.entered_at >= since,
    )
    if workspace_id is not None:
        attempt_query = attempt_query.filter(QueueAttempt.workspace_id == workspace_id)
    attempts = attempt_query.all()
    offered = len(attempts)
    answered = sum(1 for attempt in attempts if attempt.accepted_at is not None)
    abandoned = sum(
        1 for attempt in attempts
        if attempt.accepted_at is None
        and attempt.exit_reason == 'abandoned_in_queue'
    )

    has_attempt = db.session.query(QueueAttempt.id).filter(
        QueueAttempt.call_id == Call.id,
    ).exists()
    legacy_query = Call.query.filter(
        Call.queue_id == queue_slug,
        Call.created_at >= since,
        ~has_attempt,
    )
    if workspace_id is not None:
        legacy_query = legacy_query.filter(Call.workspace_id == workspace_id)
    legacy = legacy_query.all()
    return {
        'offered': offered + len(legacy),
        'answered': answered + sum(1 for call in legacy if call.answered_at is not None),
        'abandoned': abandoned + sum(
            1 for call in legacy if call.end_reason == 'abandoned_in_queue'
        ),
    }
