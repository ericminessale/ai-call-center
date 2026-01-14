from datetime import datetime
from app import db


class ConferenceParticipant(db.Model):
    """Model to track participants in a conference."""

    __tablename__ = 'conference_participants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conference_id = db.Column(db.Integer, db.ForeignKey('conferences.id'), nullable=False, index=True)
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=True)  # Link to Call record if applicable

    # Participant identification
    participant_type = db.Column(db.String(50), nullable=False)  # 'customer', 'agent', 'ai', 'supervisor'
    participant_id = db.Column(db.String(255), nullable=False)  # user_id for agents, or generated ID for customers
    call_sid = db.Column(db.String(255), nullable=True)  # SignalWire call SID for this participant
    direction = db.Column(db.String(20), nullable=True)  # 'inbound' or 'outbound' - for reporting/debugging

    # Status
    status = db.Column(db.String(50), default='joining')  # 'joining', 'active', 'left', 'muted'
    joined_at = db.Column(db.DateTime, nullable=True)
    left_at = db.Column(db.DateTime, nullable=True)
    duration = db.Column(db.Integer, nullable=True)  # Duration in seconds

    # Audio state
    is_muted = db.Column(db.Boolean, default=False)
    is_deaf = db.Column(db.Boolean, default=False)  # Can't hear others

    # Relationships
    call = db.relationship('Call', backref=db.backref('conference_participations', lazy='dynamic'))

    def __repr__(self):
        return f'<ConferenceParticipant {self.participant_type}:{self.participant_id} in conf {self.conference_id}>'

    def to_dict(self):
        """Convert participant to dictionary."""
        data = {
            'id': self.id,
            'conferenceId': self.conference_id,
            'callId': self.call_id,
            'participantType': self.participant_type,
            'participantId': self.participant_id,
            'callSid': self.call_sid,
            'direction': self.direction,
            'status': self.status,
            'joinedAt': self.joined_at.isoformat() if self.joined_at else None,
            'leftAt': self.left_at.isoformat() if self.left_at else None,
            'duration': self.duration,
            'isMuted': self.is_muted,
            'isDeaf': self.is_deaf,
        }
        return data

    def join(self):
        """Mark participant as joined."""
        self.status = 'active'
        self.joined_at = datetime.utcnow()

    def leave(self):
        """Mark participant as left."""
        self.status = 'left'
        self.left_at = datetime.utcnow()
        if self.joined_at:
            delta = self.left_at - self.joined_at
            self.duration = int(delta.total_seconds())

    def mute(self, muted=True):
        """Mute or unmute participant."""
        self.is_muted = muted
        if muted:
            self.status = 'muted'
        elif self.status == 'muted':
            self.status = 'active'

    @classmethod
    def get_by_call_sid(cls, call_sid):
        """Get participant by their call SID."""
        return db.session.query(cls).filter_by(call_sid=call_sid).first()

    @classmethod
    def get_active_by_call_sid(cls, call_sid):
        """Get an active participant by their call SID."""
        return db.session.query(cls).filter_by(
            call_sid=call_sid,
            status='active'
        ).first()

    @classmethod
    def create_agent_participant(cls, conference_id, user_id, call_sid=None, direction='outbound'):
        """Create an agent participant.

        Direction is typically 'outbound' since agents dial into their conference.
        """
        participant = cls(
            conference_id=conference_id,
            participant_type='agent',
            participant_id=str(user_id),
            call_sid=call_sid,
            direction=direction,
            status='joining'
        )
        db.session.add(participant)
        return participant

    @classmethod
    def create_customer_participant(cls, conference_id, call_id, call_sid=None, participant_id=None, direction='inbound'):
        """Create a customer participant.

        Direction is typically 'inbound' since customers called in originally.
        """
        if not participant_id:
            participant_id = f'customer-{call_id}'

        participant = cls(
            conference_id=conference_id,
            call_id=call_id,
            participant_type='customer',
            participant_id=participant_id,
            call_sid=call_sid,
            direction=direction,
            status='joining'
        )
        db.session.add(participant)
        return participant

    @classmethod
    def create_ai_participant(cls, conference_id, ai_agent_name, call_sid=None, direction='outbound'):
        """Create an AI agent participant.

        Direction is typically 'outbound' since AI is added to handle the call.
        """
        participant = cls(
            conference_id=conference_id,
            participant_type='ai',
            participant_id=ai_agent_name,
            call_sid=call_sid,
            direction=direction,
            status='joining'
        )
        db.session.add(participant)
        return participant

    @classmethod
    def find_active_for_call(cls, call_id):
        """Find the active participant record for a call."""
        return db.session.query(cls).filter_by(
            call_id=call_id,
            status='active'
        ).first()
