from datetime import datetime
from app import db


class SystemConfig(db.Model):
    """Key-value store for system configuration settings.

    Tenancy (Phase 1): the PK is composite ``(workspace_id, key)``.
    ``workspace_id = 0`` rows are the GLOBAL platform defaults (the
    pre-tenancy rows were re-keyed to 0 by migration u1v2w3x4y5z6).
    Resolution is copy-on-write layering: ``get`` prefers the current
    workspace's row and falls back to the global row; ``set`` writes to
    the current workspace (or global when no workspace context exists —
    which preserves the clone-and-own admin-settings behavior exactly).

    Deliberately NOT WorkspaceScoped: the auto-filter would hide the
    global fallback rows from workspace-scoped requests. All access goes
    through ``get``/``set`` which do the two-row resolution explicitly.
    """

    __tablename__ = 'system_config'

    GLOBAL_WORKSPACE_ID = 0

    workspace_id = db.Column(db.Integer, primary_key=True, default=GLOBAL_WORKSPACE_ID)
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    @classmethod
    def _resolve_workspace_id(cls, workspace_id):
        if workspace_id is not None:
            return int(workspace_id)
        from app.tenancy import current_workspace_id
        ws = current_workspace_id()
        return int(ws) if ws is not None else cls.GLOBAL_WORKSPACE_ID

    @classmethod
    def _row(cls, workspace_id, key):
        return cls.query.filter_by(workspace_id=workspace_id, key=key).first()

    @classmethod
    def get(cls, key, default=None, workspace_id=None):
        """Get a config value: workspace row first, then the global default."""
        ws = cls._resolve_workspace_id(workspace_id)
        if ws != cls.GLOBAL_WORKSPACE_ID:
            row = cls._row(ws, key)
            if row is not None:
                return row.value
        row = cls._row(cls.GLOBAL_WORKSPACE_ID, key)
        return row.value if row else default

    @classmethod
    def set(cls, key, value, user_id=None, workspace_id=None):
        """Set a config value in the current workspace layer (copy-on-write)."""
        ws = cls._resolve_workspace_id(workspace_id)
        row = cls._row(ws, key)
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
            row.updated_by = user_id
        else:
            row = cls(workspace_id=ws, key=key, value=value, updated_by=user_id)
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
