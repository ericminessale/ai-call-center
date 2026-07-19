from datetime import datetime

from app import db
from app.tenancy import WorkspaceScoped


class QueueAttempt(WorkspaceScoped, db.Model):
    """One continuous stay in a queue, from entry through its outcome.

    A returned call receives a new attempt while retaining ``service_started_at``
    from its first attempt. That keeps SLA wait honest without losing the
    operational detail that multiple agents may have touched the interaction.
    """

    __tablename__ = 'queue_attempts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    call_id = db.Column(
        db.Integer, db.ForeignKey('calls.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    queue_id = db.Column(
        db.Integer, db.ForeignKey('queues.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    queue_slug = db.Column(db.String(50), nullable=False)
    attempt_number = db.Column(db.Integer, nullable=False)
    priority = db.Column(db.Integer, nullable=True)
    routing_strategy = db.Column(db.String(30), nullable=True)
    transport = db.Column(db.String(20), nullable=True)

    entered_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # The first queue-entry timestamp across returns. SLA is measured from this
    # value, while entered_at remains the timestamp for this individual stay.
    service_started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    first_offered_at = db.Column(db.DateTime, nullable=True)
    last_offered_at = db.Column(db.DateTime, nullable=True)
    offer_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    last_offered_agent_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
    )
    last_declined_at = db.Column(db.DateTime, nullable=True)
    declined_offer_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    last_declined_agent_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
    )
    accepted_at = db.Column(db.DateTime, nullable=True)
    accepted_agent_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
    )
    exited_at = db.Column(db.DateTime, nullable=True)
    exit_reason = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('call_id', 'attempt_number', name='uq_queue_attempt_call_number'),
        db.Index('ix_queue_attempt_ws_queue_entered', 'workspace_id', 'queue_id', 'entered_at'),
        db.Index('ix_queue_attempt_ws_slug_accepted', 'workspace_id', 'queue_slug', 'accepted_at'),
        db.Index('ix_queue_attempt_call_exited', 'call_id', 'exited_at'),
    )

    handling_segments = db.relationship(
        'HandlingSegment', back_populates='queue_attempt',
        lazy='dynamic',
    )

    @property
    def wait_seconds(self):
        endpoint = self.accepted_at or self.exited_at
        if not endpoint or not self.service_started_at:
            return None
        return max(0, int((endpoint - self.service_started_at).total_seconds()))

    def to_dict(self):
        return {
            'id': self.id,
            'callId': self.call_id,
            'queueId': self.queue_id,
            'queueSlug': self.queue_slug,
            'attemptNumber': self.attempt_number,
            'priority': self.priority,
            'routingStrategy': self.routing_strategy,
            'transport': self.transport,
            'enteredAt': self.entered_at.isoformat() if self.entered_at else None,
            'serviceStartedAt': self.service_started_at.isoformat() if self.service_started_at else None,
            'firstOfferedAt': self.first_offered_at.isoformat() if self.first_offered_at else None,
            'lastOfferedAt': self.last_offered_at.isoformat() if self.last_offered_at else None,
            'lastOfferedAgentId': self.last_offered_agent_id,
            'offerCount': self.offer_count or 0,
            'declinedOfferCount': self.declined_offer_count or 0,
            'lastDeclinedAt': self.last_declined_at.isoformat() if self.last_declined_at else None,
            'lastDeclinedAgentId': self.last_declined_agent_id,
            'acceptedAt': self.accepted_at.isoformat() if self.accepted_at else None,
            'acceptedAgentId': self.accepted_agent_id,
            'exitedAt': self.exited_at.isoformat() if self.exited_at else None,
            'exitReason': self.exit_reason,
            'waitSeconds': self.wait_seconds,
        }
