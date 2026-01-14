from datetime import datetime
from app import db


class CallLeg(db.Model):
    """Model to track individual segments/legs of a call as it moves between handlers."""

    __tablename__ = 'call_legs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Human agent (null for AI legs)

    # Leg identification
    leg_type = db.Column(db.String(50), nullable=False)  # 'ai_agent', 'human_agent', 'transfer'
    leg_number = db.Column(db.Integer, default=1)  # Order in the call chain (1, 2, 3...)

    # AI info (if leg_type='ai_agent')
    ai_agent_name = db.Column(db.String(100), nullable=True)

    # Status
    status = db.Column(db.String(50), default='active')  # 'connecting', 'active', 'completed'
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration = db.Column(db.Integer, nullable=True)  # Duration in seconds

    # Transition info
    transition_reason = db.Column(db.String(100), nullable=True)  # 'takeover', 'transfer', 'customer_request', 'hangup'

    # Summary for this leg (can be AI-generated)
    summary = db.Column(db.Text, nullable=True)

    # Conference tracking
    conference_id = db.Column(db.Integer, db.ForeignKey('conferences.id'), nullable=True)
    conference_name = db.Column(db.String(255), nullable=True)

    # Relationships
    call = db.relationship('Call', backref=db.backref('legs', lazy='dynamic', order_by='CallLeg.leg_number'))
    user = db.relationship('User', backref=db.backref('call_legs', lazy='dynamic'))
    conference = db.relationship('Conference', backref=db.backref('call_legs', lazy='dynamic'))

    def __repr__(self):
        return f'<CallLeg {self.id} - {self.leg_type} #{self.leg_number}>'

    def to_dict(self):
        """Convert call leg to dictionary."""
        data = {
            'id': self.id,
            'callId': self.call_id,
            'userId': self.user_id,
            'legType': self.leg_type,
            'legNumber': self.leg_number,
            'aiAgentName': self.ai_agent_name,
            'status': self.status,
            'startedAt': self.started_at.isoformat() if self.started_at else None,
            'endedAt': self.ended_at.isoformat() if self.ended_at else None,
            'duration': self.duration,
            'transitionReason': self.transition_reason,
            'summary': self.summary,
            'conferenceId': self.conference_id,
            'conferenceName': self.conference_name,
        }

        # Include user info if this is a human agent leg
        if self.user:
            data['userName'] = self.user.email  # Could be extended to use a display name

        return data

    def end_leg(self, reason=None):
        """End this leg and calculate duration."""
        self.status = 'completed'
        self.ended_at = datetime.utcnow()
        if self.started_at:
            delta = self.ended_at - self.started_at
            self.duration = int(delta.total_seconds())
        if reason:
            self.transition_reason = reason

    @classmethod
    def get_active_leg(cls, call_id):
        """Get the currently active leg for a call."""
        return db.session.query(cls).filter_by(
            call_id=call_id,
            status='active'
        ).first()

    @classmethod
    def get_legs_for_call(cls, call_id):
        """Get all legs for a call, ordered by leg number."""
        return db.session.query(cls).filter_by(
            call_id=call_id
        ).order_by(cls.leg_number).all()

    @classmethod
    def create_initial_leg(cls, call, leg_type='ai_agent', ai_agent_name=None, user_id=None,
                          conference_id=None, conference_name=None):
        """Create the first leg for a call."""
        leg = cls(
            call_id=call.id,
            leg_type=leg_type,
            leg_number=1,
            ai_agent_name=ai_agent_name,
            user_id=user_id,
            status='active',
            conference_id=conference_id,
            conference_name=conference_name
        )
        db.session.add(leg)
        return leg

    @classmethod
    def create_next_leg(cls, call, leg_type, user_id=None, ai_agent_name=None,
                       conference_id=None, conference_name=None, transition_reason=None):
        """Create the next leg in the chain, ending the current active leg."""
        # End current active leg
        current_leg = cls.get_active_leg(call.id)
        next_leg_number = 1

        if current_leg:
            reason = transition_reason or ('takeover' if leg_type == 'human_agent' else 'transfer')
            current_leg.end_leg(reason=reason)
            next_leg_number = current_leg.leg_number + 1

        # Create new leg
        new_leg = cls(
            call_id=call.id,
            leg_type=leg_type,
            leg_number=next_leg_number,
            user_id=user_id,
            ai_agent_name=ai_agent_name,
            status='connecting',  # Will become 'active' when connected
            conference_id=conference_id,
            conference_name=conference_name
        )
        db.session.add(new_leg)
        return new_leg
