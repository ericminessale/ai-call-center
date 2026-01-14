from datetime import datetime
from app import db
import json


class WebhookEvent(db.Model):
    """WebhookEvent model to log all webhook events."""

    __tablename__ = 'webhook_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=True)
    event_type = db.Column(db.String(100), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    processed = db.Column(db.Boolean, default=False)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<WebhookEvent {self.event_type} - {self.id}>'

    def to_dict(self):
        """Convert webhook event to dictionary."""
        return {
            'id': self.id,
            'call_id': self.call_id,
            'event_type': self.event_type,
            'payload': self.payload,
            'processed': self.processed,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    @classmethod
    def log_event(cls, event_type, payload, call_id=None):
        """Log a webhook event."""
        event = cls(
            event_type=event_type,
            payload=payload,
            call_id=call_id
        )
        db.session.add(event)
        db.session.commit()
        return event

    @classmethod
    def find_by_call(cls, call_id):
        """Find all webhook events for a call."""
        return db.session.query(cls).filter_by(call_id=call_id).order_by(cls.created_at.desc()).all()

    def mark_processed(self, error_message=None):
        """Mark event as processed."""
        self.processed = True
        if error_message:
            self.error_message = error_message
        db.session.commit()