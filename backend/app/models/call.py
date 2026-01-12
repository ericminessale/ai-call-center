from datetime import datetime
from app import db
import json


class Call(db.Model):
    """Call model to track SignalWire calls."""

    __tablename__ = 'calls'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey('contacts.id'), nullable=True, index=True)  # Link to contact (customer)
    signalwire_call_sid = db.Column(db.String(255), unique=True, index=True)  # IMPORTANT: Stores SignalWire call_id (not SID - that's Twilio terminology)
    from_number = db.Column(db.String(255))  # Caller's phone number (inbound calls)
    destination = db.Column(db.String(255), nullable=False)  # Number called (our SignalWire number for inbound, or number we called for outbound)
    destination_type = db.Column(db.String(20), nullable=False)  # 'phone' or 'sip'
    direction = db.Column(db.String(10), default='outbound')  # 'inbound' or 'outbound'
    handler_type = db.Column(db.String(10), default='human')  # 'human' or 'ai'
    ai_agent_name = db.Column(db.String(100), nullable=True)  # Name of AI agent if handler_type='ai'
    status = db.Column(db.String(50), default='initiated')
    transcription_active = db.Column(db.Boolean, default=False, nullable=False)
    recording_url = db.Column(db.Text)  # URL to the recording
    summary = db.Column(db.Text)  # AI-generated summary
    duration = db.Column(db.Integer)  # Duration in seconds
    sentiment_score = db.Column(db.Float, nullable=True)  # -1.0 to 1.0
    ai_context = db.Column(db.Text, nullable=True)  # JSON: context from AI (goal, extracted info, etc.)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    answered_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)

    # Relationships
    transcriptions = db.relationship('Transcription', backref='call', lazy='dynamic', cascade='all, delete-orphan')
    webhook_events = db.relationship('WebhookEvent', backref='call', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Call {self.signalwire_call_sid}>'

    @property
    def ai_context_dict(self):
        """Get AI context as a dict."""
        if not self.ai_context:
            return {}
        try:
            return json.loads(self.ai_context)
        except (json.JSONDecodeError, TypeError):
            return {}

    @ai_context_dict.setter
    def ai_context_dict(self, value):
        """Set AI context from a dict."""
        self.ai_context = json.dumps(value) if value else None

    def to_dict(self, include_contact=False):
        """Convert call to dictionary."""
        data = {
            'id': self.id,
            'userId': self.user_id,
            'contactId': self.contact_id,
            'signalwireCallSid': self.signalwire_call_sid,
            'fromNumber': self.from_number,
            'destination': self.destination,
            'destinationType': self.destination_type,
            'direction': self.direction,
            'handlerType': self.handler_type,
            'aiAgentName': self.ai_agent_name,
            'status': self.status,
            'transcriptionActive': self.transcription_active,
            'recordingUrl': self.recording_url,
            'summary': self.summary,
            'duration': self.duration,
            'sentimentScore': self.sentiment_score,
            'aiContext': self.ai_context_dict,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'answeredAt': self.answered_at.isoformat() if self.answered_at else None,
            'endedAt': self.ended_at.isoformat() if self.ended_at else None,
        }

        if include_contact and self.contact:
            data['contact'] = self.contact.to_dict_minimal()

        return data

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