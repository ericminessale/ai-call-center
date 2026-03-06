from datetime import datetime
from app import db


class Queue(db.Model):
    """Configurable call queue."""

    __tablename__ = 'queues'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slug = db.Column(db.String(50), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Routing strategy: 'fifo', 'round_robin', 'priority', 'skill_based'
    routing_strategy = db.Column(db.String(30), default='round_robin', nullable=False)

    # AI agent fallback route for overflow (e.g. '/sales-ai')
    ai_agent_route = db.Column(db.String(100), nullable=True)

    default_priority = db.Column(db.Integer, default=5)
    sla_threshold_seconds = db.Column(db.Integer, default=60)
    max_wait_before_ai_fallback = db.Column(db.Integer, default=120)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent_assignments = db.relationship(
        'QueueAgentAssignment', backref='queue',
        cascade='all, delete-orphan', lazy='dynamic'
    )

    def to_dict(self, include_agent_count=False):
        result = {
            'id': self.id,
            'slug': self.slug,
            'display_name': self.display_name,
            'description': self.description,
            'is_active': self.is_active,
            'routing_strategy': self.routing_strategy,
            'ai_agent_route': self.ai_agent_route,
            'default_priority': self.default_priority,
            'sla_threshold_seconds': self.sla_threshold_seconds,
            'max_wait_before_ai_fallback': self.max_wait_before_ai_fallback,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_agent_count:
            result['agent_count'] = self.agent_assignments.count()
        return result

    @classmethod
    def find_by_slug(cls, slug):
        return cls.query.filter_by(slug=slug).first()

    @classmethod
    def get_active_slugs(cls):
        return [q.slug for q in cls.query.filter_by(is_active=True).all()]

    @classmethod
    def get_active_queues(cls):
        return cls.query.filter_by(is_active=True).order_by(cls.display_name).all()


class QueueAgentAssignment(db.Model):
    """Maps human agents (users) to queues they can service."""

    __tablename__ = 'queue_agent_assignments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    queue_id = db.Column(db.Integer, db.ForeignKey('queues.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    skill_level = db.Column(db.Integer, default=5)
    is_activated = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('queue_id', 'user_id'),)

    user = db.relationship('User', backref=db.backref('queue_assignments', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'queue_id': self.queue_id,
            'queue_slug': self.queue.slug if self.queue else None,
            'queue_display_name': self.queue.display_name if self.queue else None,
            'routing_strategy': self.queue.routing_strategy if self.queue else None,
            'user_id': self.user_id,
            'user_name': self.user.name if self.user else None,
            'user_email': self.user.email if self.user else None,
            'skill_level': self.skill_level,
            'is_activated': self.is_activated,
        }
