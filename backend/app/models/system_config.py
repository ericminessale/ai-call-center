from datetime import datetime
from app import db


class SystemConfig(db.Model):
    """Key-value store for system configuration settings."""

    __tablename__ = 'system_config'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    @classmethod
    def get(cls, key, default=None):
        """Get a config value by key, returning default if not found."""
        row = cls.query.get(key)
        return row.value if row else default

    @classmethod
    def set(cls, key, value, user_id=None):
        """Set a config value, creating or updating as needed."""
        row = cls.query.get(key)
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
            row.updated_by = user_id
        else:
            row = cls(key=key, value=value, updated_by=user_id)
            db.session.add(row)
        db.session.commit()
        return row

    @classmethod
    def get_routing_config(cls):
        """Get all routing config as a dict."""
        return {
            'initial_handler': cls.get('route.initial_handler', '/receptionist'),
            'sales_specialist': cls.get('route.sales_specialist', '/sales-ai'),
            'support_specialist': cls.get('route.support_specialist', '/support-ai'),
        }

    # White-label branding (IMP-02). None means "stock SignalWire" for that
    # field — the frontend only overrides what's actually set. Saving an
    # empty string clears a field back to stock.
    BRANDING_FIELDS = (
        'product_name', 'logo_url',
        'color_primary', 'color_accent', 'color_highlight',
    )

    @classmethod
    def get_branding_config(cls):
        out = {}
        for field in cls.BRANDING_FIELDS:
            value = cls.get(f'branding.{field}')
            out[field] = value.strip() if value and value.strip() else None
        out['enabled'] = any(out[field] for field in cls.BRANDING_FIELDS)
        return out

    def to_dict(self):
        return {
            'key': self.key,
            'value': self.value,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
