from datetime import datetime
from app import db


class Call(db.Model):
    """Call model to track SignalWire calls."""

    __tablename__ = 'calls'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    signalwire_call_sid = db.Column(db.String(255), unique=True, index=True)  # IMPORTANT: Stores SignalWire call_id (not SID - that's Twilio terminology)
    from_number = db.Column(db.String(255))  # Caller's phone number (inbound calls)
    destination = db.Column(db.String(255), nullable=False)  # Number called (our SignalWire number for inbound, or number we called for outbound)
    destination_type = db.Column(db.String(20), nullable=False)  # 'phone' or 'sip'
    status = db.Column(db.String(50), default='initiated')
    transcription_active = db.Column(db.Boolean, default=False, nullable=False)
    recording_url = db.Column(db.Text)  # URL to the recording
    summary = db.Column(db.Text)  # AI-generated summary
    duration = db.Column(db.Integer)  # Duration in seconds
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    answered_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)

    # Relationships
    transcriptions = db.relationship('Transcription', backref='call', lazy='dynamic', cascade='all, delete-orphan')
    webhook_events = db.relationship('WebhookEvent', backref='call', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Call {self.signalwire_call_sid}>'

    def to_dict(self):
        """Convert call to dictionary."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'signalwire_call_sid': self.signalwire_call_sid,
            'from_number': self.from_number,
            'destination': self.destination,
            'destination_type': self.destination_type,
            'status': self.status,
            'transcription_active': self.transcription_active,
            'recording_url': self.recording_url,
            'summary': self.summary,
            'duration': self.duration,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'answered_at': self.answered_at.isoformat() if self.answered_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None
        }

    @classmethod
    def find_by_sid(cls, call_sid):
        """Find call by SignalWire call_id (despite the method name, we search by call_id not SID)."""
        return db.session.query(cls).filter_by(signalwire_call_sid=call_sid).first()

    @classmethod
    def find_by_user(cls, user_id):
        """Find all calls for a user."""
        return db.session.query(cls).filter_by(user_id=user_id).order_by(cls.created_at.desc()).all()

    def update_status(self, status):
        """Update call status and set timestamps."""
        self.status = status
        if status == 'answered' and not self.answered_at:
            self.answered_at = datetime.utcnow()
        elif status == 'ended' and not self.ended_at:
            self.ended_at = datetime.utcnow()
            if self.answered_at:
                delta = self.ended_at - self.answered_at
                self.duration = int(delta.total_seconds())