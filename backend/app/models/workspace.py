import uuid
from datetime import datetime

from app import db


class Workspace(db.Model):
    """A tenant — one visitor's (or one deployment's) whole call center.

    Hosted tenancy (TENANCY_MODE=true): one row per visitor, provisioned on
    demand by :mod:`app.services.workspace_provision`, bound to the visitor's
    anonymous browser cookie via ``session_token_hash`` and reclaimable for
    ``WORKSPACE_TTL_DAYS`` after their last activity.

    Clone-and-own (flag off): exactly one row — the default workspace
    (id 1, created by migration u1v2w3x4y5z6) — that all data attaches to.
    In tenancy mode that same row doubles as the TEMPLATE the provisioner
    clones queues/KB/config from; it is never leased to a visitor.

    Deliberately NOT WorkspaceScoped — it is the tenancy root.
    """

    __tablename__ = 'workspaces'

    STATUS_ACTIVE = 'active'
    STATUS_EXPIRED = 'expired'
    STATUS_REAPED = 'reaped'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Opaque external identity — used in JWT ``wsid`` claims and (later)
    # URLs. Never expose the integer id outside the backend.
    public_id = db.Column(
        db.String(36), unique=True, nullable=False, index=True,
        default=lambda: str(uuid.uuid4()),
    )
    name = db.Column(db.String(200), nullable=False, default='My Call Center')
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_active_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # NULL = never expires (the default/template workspace).
    expires_at = db.Column(db.DateTime, nullable=True)
    # sha256 hex of the visitor's anonymous session cookie token — the
    # cookie→workspace binding survives Redis flushes because it lives here.
    session_token_hash = db.Column(db.String(64), unique=True, nullable=True, index=True)
    # Denormalized mirror of the Redis number→workspace verify binding, for
    # recovery / re-claim after Redis expiry (re-keyed in Phase 4).
    verified_number = db.Column(db.String(32), nullable=True, index=True)

    def is_live(self) -> bool:
        """Active and not past its idle expiry."""
        if self.status != self.STATUS_ACTIVE:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.utcnow()

    @classmethod
    def find_by_public_id(cls, public_id):
        if not public_id:
            return None
        return cls.query.filter_by(public_id=str(public_id)).first()

    def to_dict(self):
        return {
            'id': self.public_id,
            'name': self.name,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }

    def __repr__(self):
        return f'<Workspace {self.id} {self.public_id} {self.status}>'
