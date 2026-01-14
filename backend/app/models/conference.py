from datetime import datetime
from app import db


class Conference(db.Model):
    """Model to track active conferences for agents and AI handlers."""

    __tablename__ = 'conferences'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conference_name = db.Column(db.String(255), unique=True, nullable=False, index=True)
    conference_type = db.Column(db.String(50), nullable=False)  # 'agent', 'ai', 'hold'

    # Owner info (mutually exclusive based on type)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # For agent conferences
    owner_ai_agent = db.Column(db.String(100), nullable=True)  # For AI conferences (e.g., 'receptionist')
    queue_id = db.Column(db.String(50), nullable=True)  # For hold conferences

    # Status
    status = db.Column(db.String(50), default='active')  # 'active', 'ended'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    owner = db.relationship('User', backref=db.backref('conferences', lazy='dynamic'))
    participants = db.relationship('ConferenceParticipant', backref='conference', lazy='dynamic',
                                   order_by='ConferenceParticipant.joined_at')

    def __repr__(self):
        return f'<Conference {self.conference_name} ({self.conference_type})>'

    def to_dict(self, include_participants=False):
        """Convert conference to dictionary."""
        data = {
            'id': self.id,
            'conferenceName': self.conference_name,
            'conferenceType': self.conference_type,
            'ownerUserId': self.owner_user_id,
            'ownerAiAgent': self.owner_ai_agent,
            'queueId': self.queue_id,
            'status': self.status,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'endedAt': self.ended_at.isoformat() if self.ended_at else None,
        }

        if include_participants:
            data['participants'] = [p.to_dict() for p in self.participants.filter_by(status='active').all()]

        return data

    def end_conference(self):
        """End the conference."""
        self.status = 'ended'
        self.ended_at = datetime.utcnow()
        # End all active participants
        for participant in self.participants.filter_by(status='active').all():
            participant.leave()

    @classmethod
    def get_by_name(cls, conference_name):
        """Get a conference by its name."""
        return db.session.query(cls).filter_by(conference_name=conference_name).first()

    @classmethod
    def get_active_by_name(cls, conference_name):
        """Get an active conference by its name."""
        return db.session.query(cls).filter_by(
            conference_name=conference_name,
            status='active'
        ).first()

    @classmethod
    def get_or_create_agent_conference(cls, user_id):
        """Get or create an agent's personal conference."""
        conference_name = f'agent-conf-{user_id}'
        conference = cls.get_active_by_name(conference_name)

        if not conference:
            conference = cls(
                conference_name=conference_name,
                conference_type='agent',
                owner_user_id=user_id,
                status='active'
            )
            db.session.add(conference)
            db.session.flush()  # Get the ID

        return conference

    @classmethod
    def get_or_create_ai_conference(cls, ai_agent_name):
        """Get or create an AI agent's conference."""
        conference_name = f'ai-conf-{ai_agent_name}'
        conference = cls.get_active_by_name(conference_name)

        if not conference:
            conference = cls(
                conference_name=conference_name,
                conference_type='ai',
                owner_ai_agent=ai_agent_name,
                status='active'
            )
            db.session.add(conference)
            db.session.flush()

        return conference

    @classmethod
    def get_or_create_hold_conference(cls, queue_id):
        """Get or create a hold conference for a queue."""
        conference_name = f'hold-conf-{queue_id}'
        conference = cls.get_active_by_name(conference_name)

        if not conference:
            conference = cls(
                conference_name=conference_name,
                conference_type='hold',
                queue_id=queue_id,
                status='active'
            )
            db.session.add(conference)
            db.session.flush()

        return conference

    def get_active_participant_count(self):
        """Get the count of active participants."""
        return self.participants.filter_by(status='active').count()

    def get_customer_participant(self):
        """Get the customer participant if one exists."""
        return self.participants.filter_by(
            participant_type='customer',
            status='active'
        ).first()

    def get_agent_participant(self):
        """Get the agent participant if one exists."""
        return self.participants.filter_by(
            participant_type='agent',
            status='active'
        ).first()
