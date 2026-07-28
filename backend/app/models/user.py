from datetime import datetime
from app import db, bcrypt
from app.tenancy import WorkspaceScoped
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
    'can_use_coach',           # attach the AI Coach sidecar to their own calls (per-call toggle)
    'can_return_to_queue',     # bounce an accepted call back to queue routing (Tier 2p)
)

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
# 'admin'      — full workspace administrator: clone-and-own owners and the
#                platform operator (workspace_id NULL).
# 'supervisor' — monitors/coaches the floor; no configuration surface.
# 'agent'      — handles calls.
# 'visitor'    — HOSTED-DEMO workspace owner (HIGH-3). Operationally the same
#                as a supervisor *inside their own workspace* — they have to be
#                able to drive the whole demo (take calls, listen/whisper/barge,
#                edit their own queues and knowledge base) — but deliberately
#                NOT an 'admin', so the admin-MANAGEMENT routes (user CRUD,
#                permission grants, MCP-gateway CRUD/probe, bulk deletes) stay
#                closed to an anonymous member of the public. Provisioning is
#                the only thing that mints it; it is intentionally absent from
#                VALID_USER_ROLES (api/admin.py) so it can't be assigned — or
#                escalated away from — through the User Management UI.
ROLE_ADMIN = 'admin'
ROLE_SUPERVISOR = 'supervisor'
ROLE_AGENT = 'agent'
ROLE_VISITOR = 'visitor'

# Roles with supervisory reach across their whole workspace: they may see and
# act on calls they don't personally own. Replaces the ('admin', 'supervisor')
# literal that used to be spelled out at a dozen call sites — 'visitor' MUST be
# in this set or a hosted visitor loses control of their own demo floor.
SUPERVISORY_ROLES = (ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VISITOR)

# Roles allowed onto /api/admin/* at all (the blueprint-level gate).
ADMIN_SURFACE_ROLES = (ROLE_ADMIN, ROLE_VISITOR)

# Roles allowed to perform admin MANAGEMENT actions on that surface. Visitors
# are excluded on purpose — see require_full_admin in app/utils/decorators.py.
FULL_ADMIN_ROLES = (ROLE_ADMIN,)

# Roles a hosted workspace's owner row can carry: 'visitor' since HIGH-3,
# 'admin' for workspaces provisioned before it (and after a migration
# downgrade). Keep the lookup in workspace_provision tolerant of both.
WORKSPACE_OWNER_ROLES = (ROLE_VISITOR, ROLE_ADMIN)


ROLE_PERMISSION_DEFAULTS = {
    'admin': {k: True for k in PERMISSION_FLAGS},
    'supervisor': {
        'can_listen_ai_calls':    True,
        'can_listen_human_calls': True,
        'can_whisper':            True,
        'can_barge':              True,
        'can_control_recording':  True,
        'can_use_coach':          True,
        'can_return_to_queue':    True,
    },
    'agent': {
        'can_listen_ai_calls':    False,
        'can_listen_human_calls': False,
        'can_whisper':            False,
        'can_barge':              False,
        'can_control_recording':  True,   # agent can pause/resume on their own call
        'can_use_coach':          True,   # opt-in via in-call toggle; defaults to off per-call
        'can_return_to_queue':    True,   # default-on, revokable for abuse cases per 2p spec
    },
    # Hosted-demo visitor: the *capability* set of a supervisor, because the
    # visitor is the only human on their floor and every one of these flags is
    # a demo beat (Listen on the AI call, whisper/barge into a human one,
    # pause recording, bounce a call back to the queue). What a visitor can't
    # do isn't expressed here — it's the admin-management gate (HIGH-3).
    #
    # can_use_coach stays True even though the Coach is unfinished: the
    # COACH_ENABLED flag (utils/feature_flags.py) is the real gate and it is
    # off everywhere by default plus @block_in_demo_mode on the endpoints.
    # Flipping this flag off instead would also hide the deliberate
    # "AI coach — coming soon" teaser from the demo.
    'visitor': {
        'can_listen_ai_calls':    True,
        'can_listen_human_calls': True,
        'can_whisper':            True,
        'can_barge':              True,
        'can_control_recording':  True,
        'can_use_coach':          True,
        'can_return_to_queue':    True,
    },
    # (The old 'demo_agent' persona role is gone with the shared floor. Hosted
    # visitors are workspace-scoped 'visitor' users; their isolation is
    # structural — tenancy auto-filtering plus the workspace predicates at the
    # socket auth points — and their privilege ceiling is the role split above.)
}


class User(WorkspaceScoped, db.Model):
    """User model for authentication."""

    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Tenancy: NULL = platform-level user (operator admin, clone-and-own
    # users). Email uniqueness is per-workspace via a COALESCE(workspace_id,
    # 0) expression index in the DB (uq_users_workspace_email) — not
    # expressible as a plain UniqueConstraint here.
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=True, index=True,
    )
    email = db.Column(db.String(255), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    # Free-text VARCHAR, not a Postgres ENUM — adding 'visitor' needed no
    # schema change (see the role constants above for the valid values). The
    # w3x4y5z6a7b8 migration only re-labels existing rows.
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

    # Agent Assist — Knowledge Factbook mode. See AGENT_ASSIST.md.
    # 'off' hides the panel; 'manual' shows the panel with typed-query + From-transcript
    # buttons; 'auto' additionally streams KB facts on each customer turn-end (M4).
    kb_factbook_mode = db.Column(
        db.String(20), nullable=False, default='manual', server_default='manual',
    )

    # Agent Assist — AI Coach (sidecar) mode. See AGENT_ASSIST.md.
    # 'off': no sidecar attached, no billing.
    # 'on_request': sidecar attached but defaults to sidecar_skip; agent uses
    #               the "ask coach" button to pull suggestions.
    # 'auto': sidecar suggests on every customer turn.
    coach_mode = db.Column(
        db.String(20), nullable=False, default='off', server_default='off',
    )

    # Coach prompt-tone preset. Selects which of three prompt templates feeds
    # the sidecar's system prompt at attach time. terse → standard → verbose.
    coach_intensity = db.Column(
        db.String(20), nullable=False, default='standard', server_default='standard',
    )

    # Relationships
    # Calls owned by this user (user_id foreign key)
    calls = db.relationship('Call', backref='user', lazy='dynamic', cascade='all, delete-orphan',
                           foreign_keys='Call.user_id')
    # Calls assigned to this user as an agent (assigned_agent_id foreign key)
    assigned_calls = db.relationship('Call', backref='assigned_agent', lazy='dynamic',
                                     foreign_keys='Call.assigned_agent_id')

    workspace = db.relationship('Workspace', backref='users')

    def __repr__(self):
        return f'<User {self.email}>'

    def set_password(self, password):
        """Hash and set the user's password."""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """Check if the provided password matches the hash."""
        return bcrypt.check_password_hash(self.password_hash, password)

    def set_subscriber_password(self, password):
        """Encrypt and store the subscriber password.

        Delegates to :mod:`app.utils.secrets_box` so all at-rest secrets
        share one required, cached Fernet key (``SUBSCRIBER_PASSWORD_KEY``) —
        no more per-call ephemeral keys or keys printed to stdout.
        """
        from app.utils.secrets_box import encrypt_secret
        self.signalwire_password_encrypted = encrypt_secret(password or None)

    def get_subscriber_password(self):
        """Decrypt and return the subscriber password (None if unset/invalid)."""
        from app.utils.secrets_box import decrypt_secret
        return decrypt_secret(self.signalwire_password_encrypted)

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
            'workspace_id': self.workspace_id,
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
            'kb_factbook_mode': self.kb_factbook_mode or 'manual',
            'coach_mode': self.coach_mode or 'off',
            'coach_intensity': self.coach_intensity or 'standard',
        }

    @classmethod
    def find_by_email(cls, email):
        """Find user by email."""
        return db.session.query(cls).filter_by(email=email).first()

    @classmethod
    def find_by_id(cls, user_id):
        """Find user by ID."""
        return db.session.query(cls).filter_by(id=user_id).first()