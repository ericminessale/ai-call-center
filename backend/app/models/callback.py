"""Callback model — Tier 2r.

Tracks a caller's request to be called back rather than wait in queue.
Currently fed by:
  - An in-call human-agent action — agent decides to put the contact on
    the callback list mid-call (caller can't wait, queue is jammed, etc.).

An IVR fallback that lets a caller opt into a callback while on hold is
planned (roadmap Tier 1c) but not currently wired.

The row carries a snapshot of everything an agent will need at dial-back
time: the phone to call, the caller's name (best-effort), the reason
(extracted from the AI context if available), the original queue, and
the AI context dict so the next agent can pick up the thread.

Lifecycle:
  pending  → claimed → completed (with outcome)
                    → released (back to pending, available for someone else)
           → expired (auto, 24h after request)
"""
from datetime import datetime, timedelta
from app import db
import json


# Outcome values an agent can record after attempting / completing a callback.
# Stored as a string so we can extend the list without migrating.
CALLBACK_OUTCOMES = (
    'success',         # connected, issue handled
    'no-answer',       # rang out / did not pick up
    'voicemail',       # left voicemail
    'declined',        # caller answered but said "no thanks"
    'wrong-number',    # number was bad / unrelated person
    'expired',         # passed the expiry window untouched
)


# Default time-to-live for a pending callback. Agents can re-queue / extend.
DEFAULT_EXPIRY_HOURS = 24


class Callback(db.Model):
    """A caller-requested or agent-scheduled callback row."""

    __tablename__ = 'callbacks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Origin call that triggered the request. Nullable so an agent can also
    # schedule a callback without a current call (e.g. proactive outbound).
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id', ondelete='SET NULL'), nullable=True, index=True)

    # Contact CRM linkage — preferred over phone lookup at dial time.
    contact_id = db.Column(db.Integer, db.ForeignKey('contacts.id', ondelete='SET NULL'), nullable=True, index=True)

    # Originating queue (so the dial-back flows through the same queue's
    # context / AI agent assignment when the agent connects).
    queue_id = db.Column(db.String(50), nullable=True, index=True)

    # Snapshot fields — captured at request time so they don't drift if the
    # contact record is later edited.
    phone_number = db.Column(db.String(64), nullable=False)
    caller_name = db.Column(db.String(255), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    ai_context = db.Column(db.Text, nullable=True)  # JSON

    # Lifecycle timestamps
    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    claimed_by_agent_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)

    # Multi-attempt tracking
    attempts = db.Column(db.Integer, default=0, nullable=False)
    outcome = db.Column(db.String(32), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # Relationships
    call = db.relationship('Call', backref='callbacks', foreign_keys=[call_id])
    contact = db.relationship('Contact', backref='callbacks', foreign_keys=[contact_id])
    claimed_by = db.relationship('User', foreign_keys=[claimed_by_agent_id])

    def __repr__(self):
        return f'<Callback {self.id} {self.phone_number} status={self.status}>'

    # -------------------------------------------------------------------------
    # Computed state
    # -------------------------------------------------------------------------

    @property
    def is_expired(self) -> bool:
        """True if the request has passed its expiry window without completion."""
        if self.completed_at is not None:
            return False
        return datetime.utcnow() > self.expires_at

    @property
    def status(self) -> str:
        """High-level status string for UI consumption.

        Order matters — completed wins over expired (a successful callback
        outside the window still counts as success), and claimed wins over
        pending / expired (an agent is actively working it).
        """
        if self.completed_at is not None:
            return 'completed'
        if self.claimed_at is not None and self.claimed_by_agent_id is not None:
            return 'claimed'
        if self.is_expired:
            return 'expired'
        return 'pending'

    @property
    def ai_context_dict(self) -> dict:
        if not self.ai_context:
            return {}
        try:
            return json.loads(self.ai_context)
        except (json.JSONDecodeError, TypeError):
            return {}

    @ai_context_dict.setter
    def ai_context_dict(self, value):
        self.ai_context = json.dumps(value) if value else None

    # -------------------------------------------------------------------------
    # Constructors / lifecycle helpers
    # -------------------------------------------------------------------------

    @classmethod
    def create_from_call(cls, call, queue_id=None, reason=None, expiry_hours=DEFAULT_EXPIRY_HOURS):
        """Create a callback row from an existing Call, snapshotting context.

        Returns the fresh, unsaved row; caller is responsible for
        ``db.session.add(...)`` + ``commit()`` so the surrounding handler
        can decide how to bundle the work.
        """
        ctx = call.ai_context_dict if hasattr(call, 'ai_context_dict') else {}
        # Pick a good caller name from whatever the AI captured. Falls back to
        # contact name and finally None.
        caller_name = (
            ctx.get('customer_name')
            or ctx.get('caller_name')
            or (call.contact.name if getattr(call, 'contact', None) else None)
        )
        # Reason: prefer caller-provided slot; fall back to argument; final fallback empty.
        reason_text = reason or ctx.get('reason') or ctx.get('issue') or ctx.get('issue_description')

        return cls(
            call_id=call.id,
            contact_id=getattr(call, 'contact_id', None),
            queue_id=queue_id or getattr(call, 'queue_id', None),
            phone_number=call.from_number,
            caller_name=caller_name,
            reason=reason_text,
            ai_context=call.ai_context if isinstance(call.ai_context, str) else json.dumps(ctx),
            requested_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=expiry_hours),
        )

    def claim(self, agent_id):
        """Mark this callback as claimed by an agent. Idempotent."""
        self.claimed_by_agent_id = agent_id
        self.claimed_at = datetime.utcnow()

    def release(self):
        """Return a claimed callback to the pending pool."""
        self.claimed_by_agent_id = None
        self.claimed_at = None

    def complete(self, outcome, notes=None):
        """Record an outcome and mark the callback complete."""
        if outcome not in CALLBACK_OUTCOMES:
            raise ValueError(f'Unknown outcome: {outcome}')
        self.outcome = outcome
        self.completed_at = datetime.utcnow()
        if notes is not None:
            self.notes = notes

    def reopen_for_retry(self):
        """Bump attempts and clear claim so another agent (or same one) can retry."""
        self.attempts += 1
        self.claimed_by_agent_id = None
        self.claimed_at = None
        # Don't touch outcome — keep the previous attempt's record visible.

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    @classmethod
    def find_pending_for_contact(cls, contact_id):
        """Return the most-recent pending callback for a contact, or None."""
        if not contact_id:
            return None
        now = datetime.utcnow()
        return (
            db.session.query(cls)
            .filter(
                cls.contact_id == contact_id,
                cls.completed_at.is_(None),
                cls.expires_at > now,
            )
            .order_by(cls.requested_at.desc())
            .first()
        )

    @classmethod
    def find_pending(cls, queue_id=None):
        """Return all pending (not completed, not expired) callbacks, oldest first."""
        now = datetime.utcnow()
        q = db.session.query(cls).filter(
            cls.completed_at.is_(None),
            cls.expires_at > now,
        )
        if queue_id:
            q = q.filter(cls.queue_id == queue_id)
        return q.order_by(cls.requested_at.asc()).all()

    # -------------------------------------------------------------------------
    # Serialisation
    # -------------------------------------------------------------------------

    def to_dict(self, include_contact=False):
        # Re-derive `wait_minutes` so the UI doesn't have to do timezone math.
        wait_minutes = None
        if self.requested_at:
            delta = (self.completed_at or datetime.utcnow()) - self.requested_at
            wait_minutes = int(delta.total_seconds() // 60)

        data = {
            'id': self.id,
            'callId': self.call_id,
            'contactId': self.contact_id,
            'queueId': self.queue_id,
            'phoneNumber': self.phone_number,
            'callerName': self.caller_name,
            'reason': self.reason,
            'aiContext': self.ai_context_dict,
            'requestedAt': self.requested_at.isoformat() if self.requested_at else None,
            'expiresAt': self.expires_at.isoformat() if self.expires_at else None,
            'claimedByAgentId': self.claimed_by_agent_id,
            'claimedAt': self.claimed_at.isoformat() if self.claimed_at else None,
            'completedAt': self.completed_at.isoformat() if self.completed_at else None,
            'attempts': self.attempts,
            'outcome': self.outcome,
            'notes': self.notes,
            'status': self.status,
            'isExpired': self.is_expired,
            'waitMinutes': wait_minutes,
        }

        if include_contact and self.contact:
            data['contact'] = self.contact.to_dict_minimal()

        return data
