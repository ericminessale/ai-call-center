from datetime import datetime

from app import db
from app.tenancy import WorkspaceScoped


class HandlingSegment(WorkspaceScoped, db.Model):
    """A measured interval handled by AI, a human, hold, or consultation."""

    __tablename__ = 'handling_segments'

    TYPES = ('ai', 'human', 'hold', 'consultation')
    PRIMARY_TYPES = ('ai', 'human', 'hold')

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    call_id = db.Column(
        db.Integer, db.ForeignKey('calls.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    queue_attempt_id = db.Column(
        db.Integer, db.ForeignKey('queue_attempts.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    segment_type = db.Column(db.String(20), nullable=False)
    agent_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    ai_agent_name = db.Column(db.String(100), nullable=True)
    transport = db.Column(db.String(20), nullable=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)
    end_reason = db.Column(db.String(50), nullable=True)
    details = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_handling_segment_call_type_start', 'call_id', 'segment_type', 'started_at'),
        db.Index('ix_handling_segment_ws_agent_start', 'workspace_id', 'agent_id', 'started_at'),
        db.Index('ix_handling_segment_ws_agent_ended', 'workspace_id', 'agent_id', 'ended_at'),
    )

    queue_attempt = db.relationship('QueueAttempt', back_populates='handling_segments')

    @property
    def duration_seconds(self):
        if not self.ended_at or not self.started_at:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds()))

    def to_dict(self):
        return {
            'id': self.id,
            'callId': self.call_id,
            'queueAttemptId': self.queue_attempt_id,
            'type': self.segment_type,
            'agentId': self.agent_id,
            'aiAgentName': self.ai_agent_name,
            'transport': self.transport,
            'startedAt': self.started_at.isoformat() if self.started_at else None,
            'endedAt': self.ended_at.isoformat() if self.ended_at else None,
            'endReason': self.end_reason,
            'durationSeconds': self.duration_seconds,
            'details': self.details or {},
        }
