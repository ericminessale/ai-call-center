from datetime import datetime
from app import db, bcrypt
from cryptography.fernet import Fernet
from sqlalchemy.dialects.postgresql import JSON
import os
import base64


# All defined permission flags and their default value per role.
# Missing key in the user's `permissions` dict = inherit role default.
# Present key (true/false) = explicit override.
PERMISSION_FLAGS = (
    'can_listen_ai_calls',     # silently monitor calls handled by an AI agent
    'can_listen_human_calls',  # silently monitor calls handled by another human agent
    'can_whisper',             # coach an agent on an active call (one-way audio)
    'can_barge',               # insert self into an active call (full audio)
    'can_control_recording',   # start/stop recording on calls they are participating in
)

ROLE_PERMISSION_DEFAULTS = {
    'admin': {k: True for k in PERMISSION_FLAGS},
    'supervisor': {
        'can_listen_ai_calls':    True,
        'can_listen_human_calls': True,
        'can_whisper':            True,
        'can_barge':              True,
        'can_control_recording':  True,
    },
    'agent': {
        'can_listen_ai_calls':    False,
        'can_listen_human_calls': False,
        'can_whisper':            False,
        'can_barge':              False,
        'can_control_recording':  True,  # agent can pause/resume on their own call
    },
}


class User(db.Model):
    """User model for authentication."""

    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(50), default='agent', nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Call Fabric Subscriber Info
    signalwire_subscriber_id = db.Column(db.String(100), unique=True, nullable=True, index=True)
    signalwire_username = db.Column(db.String(100), unique=True, nullable=True, index=True)
    signalwire_password_encrypted = db.Column(db.String(500), nullable=True)
    signalwire_address = db.Column(db.String(255), nullable=True)
    fabric_subscriber_created_at = db.Column(db.DateTime, nullable=True)

    # Languages this agent speaks — list of BCP-47 codes used by the
    # queue router to prefer language-matched agents and decide whether
    # live_translate needs to start when a call connects.
    languages = db.Column(JSON, nullable=False, default=lambda: ['en-US'])

    # Per-user capability overrides. Empty dict = pure role defaults.
    # Key presence sets an explicit true/false regardless of role.
    # Keys are defined in ROLE_PERMISSION_DEFAULTS below; see
    # effective_permissions() for resolution order.
    permissions = db.Column(JSON, nullable=False, default=dict)

    # Relationships
    # Calls owned by this user (user_id foreign key)
    calls = db.relationship('Call', backref='user', lazy='dynamic', cascade='all, delete-orphan',
                           foreign_keys='Call.user_id')
    # Calls assigned to this user as an agent (assigned_agent_id foreign key)
    assigned_calls = db.relationship('Call', backref='assigned_agent', lazy='dynamic',
                                     foreign_keys='Call.assigned_agent_id')

    def __repr__(self):
        return f'<User {self.email}>'

    def set_password(self, password):
        """Hash and set the user's password."""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """Check if the provided password matches the hash."""
        return bcrypt.check_password_hash(self.password_hash, password)

    @staticmethod
    def _get_encryption_key():
        """Get or generate encryption key for subscriber passwords."""
        key = os.getenv('SUBSCRIBER_PASSWORD_KEY')
        if not key:
            # Generate a key if not set (for development)
            # In production, this MUST be set in environment
            key = Fernet.generate_key().decode()
            print(f"WARNING: Generated temporary encryption key. Set SUBSCRIBER_PASSWORD_KEY={key}")

        # Ensure key is properly formatted
        if isinstance(key, str):
            key = key.encode()
        return key

    def set_subscriber_password(self, password):
        """Encrypt and store subscriber password."""
        if not password:
            self.signalwire_password_encrypted = None
            return

        key = self._get_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(password.encode())
        self.signalwire_password_encrypted = encrypted.decode()

    def get_subscriber_password(self):
        """Decrypt and return subscriber password."""
        if not self.signalwire_password_encrypted:
            return None

        key = self._get_encryption_key()
        fernet = Fernet(key)
        decrypted = fernet.decrypt(self.signalwire_password_encrypted.encode())
        return decrypted.decode()

    def effective_permissions(self) -> dict:
        """Return the user's flat permission map after merging role defaults
        with per-user overrides. Always contains every key in PERMISSION_FLAGS.
        """
        defaults = ROLE_PERMISSION_DEFAULTS.get(
            self.role, ROLE_PERMISSION_DEFAULTS['agent']
        )
        overrides = self.permissions or {}
        # Role defaults first, then per-user overrides win. Unknown override
        # keys are kept as-is — forward-compat with new flags shipping before
        # the seed is updated.
        return {**defaults, **{k: bool(v) for k, v in overrides.items()}}

    def has_permission(self, flag: str) -> bool:
        return bool(self.effective_permissions().get(flag, False))

    def to_dict(self):
        """Convert user to dictionary."""
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'role': self.role,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'has_subscriber': self.signalwire_subscriber_id is not None,
            'signalwire_address': self.signalwire_address,
            'languages': self.languages or ['en-US'],
            # Flat resolved permissions — frontend gates UI off these.
            # Explicit overrides from `permissions` column are shipped too
            # so the drawer can show "overridden" state distinctly.
            'effective_permissions': self.effective_permissions(),
            'permission_overrides': self.permissions or {},
        }

    @classmethod
    def find_by_email(cls, email):
        """Find user by email."""
        return db.session.query(cls).filter_by(email=email).first()

    @classmethod
    def find_by_id(cls, user_id):
        """Find user by ID."""
        return db.session.query(cls).filter_by(id=user_id).first()