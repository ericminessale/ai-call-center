from datetime import datetime
from app import db
import json


class Contact(db.Model):
    """Contact model representing customers/callers in the call center."""

    __tablename__ = 'contacts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Identity
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    display_name = db.Column(db.String(200), nullable=True)  # Computed or manual override
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)  # Primary phone (E.164 format)
    email = db.Column(db.String(255), nullable=True, index=True)
    avatar_url = db.Column(db.String(500), nullable=True)

    # Organization
    company = db.Column(db.String(200), nullable=True)
    job_title = db.Column(db.String(100), nullable=True)

    # Account classification
    account_tier = db.Column(db.String(20), default='prospect')  # prospect, free, pro, enterprise
    account_status = db.Column(db.String(20), default='active')  # active, churned, prospect
    external_id = db.Column(db.String(100), nullable=True, index=True)  # CRM ID or external system ID

    # Flags
    is_vip = db.Column(db.Boolean, default=False, nullable=False)
    is_blocked = db.Column(db.Boolean, default=False, nullable=False)

    # Metadata
    tags = db.Column(db.Text, nullable=True)  # JSON array of tags
    notes = db.Column(db.Text, nullable=True)
    custom_fields = db.Column(db.Text, nullable=True)  # JSON object for custom data

    # Computed fields (updated by triggers/application logic)
    total_calls = db.Column(db.Integer, default=0, nullable=False)
    last_interaction_at = db.Column(db.DateTime, nullable=True)
    average_sentiment = db.Column(db.Float, nullable=True)  # -1.0 to 1.0

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    calls = db.relationship('Call', backref='contact', lazy='dynamic')

    def __repr__(self):
        return f'<Contact {self.display_name or self.phone}>'

    @property
    def computed_display_name(self):
        """Generate display name from available data."""
        if self.display_name:
            return self.display_name
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        if self.first_name:
            return self.first_name
        if self.company:
            return self.company
        return self.phone

    @property
    def tags_list(self):
        """Get tags as a list."""
        if not self.tags:
            return []
        try:
            return json.loads(self.tags)
        except (json.JSONDecodeError, TypeError):
            return []

    @tags_list.setter
    def tags_list(self, value):
        """Set tags from a list."""
        self.tags = json.dumps(value) if value else None

    @property
    def custom_fields_dict(self):
        """Get custom fields as a dict."""
        if not self.custom_fields:
            return {}
        try:
            return json.loads(self.custom_fields)
        except (json.JSONDecodeError, TypeError):
            return {}

    @custom_fields_dict.setter
    def custom_fields_dict(self, value):
        """Set custom fields from a dict."""
        self.custom_fields = json.dumps(value) if value else None

    def to_dict(self, include_stats=True):
        """Convert contact to dictionary."""
        data = {
            'id': self.id,
            'firstName': self.first_name,
            'lastName': self.last_name,
            'displayName': self.computed_display_name,
            'phone': self.phone,
            'email': self.email,
            'avatarUrl': self.avatar_url,
            'company': self.company,
            'jobTitle': self.job_title,
            'accountTier': self.account_tier,
            'accountStatus': self.account_status,
            'externalId': self.external_id,
            'isVip': self.is_vip,
            'isBlocked': self.is_blocked,
            'tags': self.tags_list,
            'notes': self.notes,
            'customFields': self.custom_fields_dict,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_stats:
            data.update({
                'totalCalls': self.total_calls,
                'lastInteractionAt': self.last_interaction_at.isoformat() if self.last_interaction_at else None,
                'averageSentiment': self.average_sentiment,
            })

        return data

    def to_dict_minimal(self):
        """Minimal dict for list views."""
        return {
            'id': self.id,
            'displayName': self.computed_display_name,
            'phone': self.phone,
            'company': self.company,
            'accountTier': self.account_tier,
            'isVip': self.is_vip,
            'totalCalls': self.total_calls,
            'lastInteractionAt': self.last_interaction_at.isoformat() if self.last_interaction_at else None,
        }

    def update_stats(self):
        """Update computed statistics from calls."""
        from sqlalchemy import func

        # Count total calls
        self.total_calls = self.calls.count()

        # Get last interaction
        last_call = self.calls.order_by(db.desc('created_at')).first()
        if last_call:
            self.last_interaction_at = last_call.created_at

        # TODO: Calculate average sentiment when we have sentiment data on calls

    @classmethod
    def find_by_phone(cls, phone):
        """Find contact by phone number."""
        # Normalize phone number (strip non-digits, ensure + prefix)
        normalized = cls.normalize_phone(phone)
        return db.session.query(cls).filter_by(phone=normalized).first()

    @classmethod
    def find_or_create_by_phone(cls, phone, **kwargs):
        """Find existing contact or create new one."""
        normalized = cls.normalize_phone(phone)
        contact = cls.find_by_phone(normalized)

        if not contact:
            contact = cls(phone=normalized, **kwargs)
            db.session.add(contact)
            db.session.commit()

        return contact

    @staticmethod
    def normalize_phone(phone):
        """Normalize phone number to E.164 format."""
        if not phone:
            return None

        # Remove all non-digit characters except leading +
        has_plus = phone.startswith('+')
        digits = ''.join(c for c in phone if c.isdigit())

        # Add + prefix if not present and looks like full number
        if has_plus or len(digits) >= 10:
            return f'+{digits}' if not has_plus else f'+{digits}'

        return digits

    @classmethod
    def search(cls, query, limit=20):
        """Search contacts by name, phone, email, or company."""
        if not query:
            return cls.query.order_by(cls.last_interaction_at.desc().nullslast()).limit(limit).all()

        search_term = f'%{query}%'
        return cls.query.filter(
            db.or_(
                cls.first_name.ilike(search_term),
                cls.last_name.ilike(search_term),
                cls.display_name.ilike(search_term),
                cls.phone.ilike(search_term),
                cls.email.ilike(search_term),
                cls.company.ilike(search_term),
            )
        ).order_by(cls.last_interaction_at.desc().nullslast()).limit(limit).all()
