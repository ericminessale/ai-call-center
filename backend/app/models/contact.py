from datetime import datetime
from app import db
from app.tenancy import WorkspaceScoped
import json


class Contact(WorkspaceScoped, db.Model):
    """Contact model representing customers/callers in the call center."""

    __tablename__ = 'contacts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Tenancy: phone uniqueness is per-workspace — the same customer number
    # can exist as a separate, private contact in every workspace.
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=False,
    )

    # Identity
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    display_name = db.Column(db.String(200), nullable=True)  # Computed or manual override
    phone = db.Column(db.String(20), nullable=False, index=True)  # Primary phone (E.164 format)
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
    # The caller's documented language (BCP-47, e.g. 'es-ES'). Durable and
    # HUMAN-SETTABLE, unlike calls.caller_language which records one call.
    # The AI seeds it only while empty and never overwrites it, so an agent's
    # assertion wins. Drives the AI opening in that language on later calls.
    preferred_language = db.Column(db.String(20), nullable=True)
    # R4: rolling caller-memory digest — JSON array (newest first, max 3) of
    # {ended_at, handler, ai_agent, reason, disposition, summary}. Regenerated
    # from Call rows at call end (services/contact_enrichment.py); consumed by
    # call-context (AI), the agent desktop, and the future chat kernel.
    interaction_digest = db.Column(db.Text, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    calls = db.relationship('Call', backref='contact', lazy='dynamic')

    __table_args__ = (db.UniqueConstraint('workspace_id', 'phone', name='uq_contacts_workspace_phone'),)

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
            'preferredLanguage': self.preferred_language,
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
                'interactionDigest': self.interaction_digest_list,
            })

        return data

    @property
    def interaction_digest_list(self):
        """Parsed interaction digest (empty list when not yet generated)."""
        if not self.interaction_digest:
            return []
        try:
            parsed = json.loads(self.interaction_digest)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

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
        """Update computed statistics from calls.

        Every aggregate is restricted to calls in THIS contact's workspace.
        The relationship itself is unfiltered, and the finalizer that calls
        this runs in webhook context with no workspace set — so without the
        explicit predicate a call mis-bound to a foreign workspace's contact
        would inflate that contact's counters even though the interaction
        digest (which does filter) correctly excludes it. Keeping both on the
        same predicate stops the counters and the digest disagreeing.
        (Verification audit B-1, 2026-08-05.)
        """
        from sqlalchemy import func

        from app.models.call import Call

        own_calls = self.calls.filter(Call.workspace_id == self.workspace_id)

        # Count total calls
        self.total_calls = own_calls.count()

        # Get last interaction
        last_call = own_calls.order_by(db.desc('created_at')).first()
        if last_call:
            self.last_interaction_at = last_call.created_at

        # Average sentiment across calls that carry a score (stays None
        # until at least one call has one)
        avg = (
            own_calls.filter(Call.sentiment_score.isnot(None))
            .with_entities(func.avg(Call.sentiment_score))
            .scalar()
        )
        self.average_sentiment = float(avg) if avg is not None else None

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
        """Canonical storage spelling for a phone number, or None.

        Delegates to :func:`app.utils.phone.normalize_phone` so there is
        exactly ONE definition of the key. This used to have its own, and the
        two disagreed: it returned BARE digits (no ``+``) for anything under
        10 digits without a leading plus, and an empty string for input with
        no digits at all — so ``Contact(phone='')`` was reachable. Rows written
        under those rules are why lookups keyed on the canonical form could
        miss an existing contact and insert a duplicate; migration
        x4y5z6a7b8c9 rewrites them.

        Returns None for anything unusable. Callers must handle that rather
        than storing it — ``phone`` is NOT NULL.
        """
        from app.utils.phone import normalize_phone as _normalize
        return _normalize(phone)

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
