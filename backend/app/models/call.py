from datetime import datetime
from app import db
from app.tenancy import WorkspaceScoped
import json


def _call_capabilities(call) -> list:
    """Return the string list of capabilities for this call.

    Wraps ``app.services.call_transport.capabilities`` with import-local
    safety: serialization shouldn't fail when capability lookup errors —
    fall back to an empty list and let the UI hide everything optional.
    """
    try:
        from app.services import call_transport
        return [c.value for c in call_transport.capabilities(call)]
    except Exception:
        return []


def _estimated_cost(call):
    """Estimated platform cost in USD (IMP-01) — None until the call has a
    duration. Same import-local guard as capabilities: serialization never
    fails on the estimator."""
    try:
        from app.services import cost_service
        return cost_service.estimate_cost_total(call)
    except Exception:
        return None


class Call(WorkspaceScoped, db.Model):
    """Call model to track SignalWire calls."""

    __tablename__ = 'calls'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Tenancy: denormalized owning workspace (hot path — attribution, list
    # filters, emit routing must not join through users). Backfilled from
    # user_id; auto-stamped at flush from context or the owning user.
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=False, index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey('contacts.id'), nullable=True, index=True)  # Link to contact (customer)
    signalwire_call_sid = db.Column(db.String(255), unique=True, index=True)  # IMPORTANT: Stores SignalWire call_id (not SID - that's Twilio terminology)
    from_number = db.Column(db.String(255))  # Caller's phone number (inbound calls)
    destination = db.Column(db.String(255), nullable=False)  # Number called (our SignalWire number for inbound, or number we called for outbound)
    destination_type = db.Column(db.String(20), nullable=False)  # 'phone' or 'sip'
    direction = db.Column(db.String(10), default='outbound')  # 'inbound' or 'outbound'
    handler_type = db.Column(db.String(10), default='human')  # 'human' or 'ai'
    ai_agent_name = db.Column(db.String(100), nullable=True)  # Name of AI agent if handler_type='ai'
    status = db.Column(db.String(50), default='initiated')
    transcription_active = db.Column(db.Boolean, default=False, nullable=False)
    recording_url = db.Column(db.Text)  # URL to the recording
    summary = db.Column(db.Text)  # AI-generated summary
    duration = db.Column(db.Integer)  # Duration in seconds
    sentiment_score = db.Column(db.Float, nullable=True)  # -1.0 to 1.0
    ai_context = db.Column(db.Text, nullable=True)  # JSON: context from AI (goal, extracted info, etc.)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    answered_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)

    # Queue tracking fields
    queue_id = db.Column(db.String(50), nullable=True, index=True)  # Which queue (sales, support, etc.)
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Agent assigned to handle
    assigned_at = db.Column(db.DateTime, nullable=True)  # When agent was notified
    conference_name = db.Column(db.String(255), nullable=True)  # Interaction conference name (null for bridge mode)

    # Call transport: 'conference' or 'bridge'. Set once at ingress in
    # call_transport.build_ingress_swml and used by every per-call op to
    # dispatch to the right implementation. See CALL_TRANSPORT.md.
    transport = db.Column(
        db.String(20), default='conference', nullable=False,
        server_default='conference',
    )

    # Translation fields — set by AI receptionist + queue router, used at conference join
    caller_language = db.Column(db.String(20), nullable=True)  # BCP-47 (e.g. "es-ES")
    needs_translation = db.Column(db.Boolean, default=False, nullable=False)

    # Wrap-up fields (Tier 2a) — set by the human agent (or supervisor) once the call ends.
    # `summary` and `ai_context` above are AI-generated; these columns are the agent's record.
    disposition_code = db.Column(db.String(50), nullable=True)  # e.g. "resolved", "callback-scheduled"
    agent_notes = db.Column(db.Text, nullable=True)  # free-text wrap-up notes
    wrapped_up_at = db.Column(db.DateTime, nullable=True)  # timestamp the wrap-up was finalized
    # Provenance of the current wrap-up values: 'ai' when auto-filled from the
    # post-prompt report, 'agent' once a human edits/saves. Drives the
    # "Captured by AI" badge explicitly, instead of inferring it from wrapped_up_at.
    wrap_up_source = db.Column(db.String(10), nullable=True)

    # Technical ending classification — HOW the call ended, distinct from
    # disposition_code (the agent's BUSINESS outcome). Computed deterministically
    # on call end (see compute_end_reason). Drives the call-history status chip.
    #   abandoned_in_queue   — caller hung up before reaching anyone
    #   missed               — agent was assigned but the call never connected
    #   premature_disconnect — connected but dropped almost immediately (<10s)
    #   caller_hangup        — connected, caller ended the call (per hangup_direction)
    #   agent_hangup         — connected, agent ended the call (per hangup_direction)
    #   completed            — connected, normal length, hangup direction unknown
    #   failed               — call failed (carrier / setup error)
    end_reason = db.Column(db.String(40), nullable=True)

    # Who ended the call first — 'caller', 'agent', or NULL (unknown). Sourced
    # from (1) the frontend signalling explicitly when the agent presses the
    # hangup button, or (2) SignalWire's call-state 'ended' payload field
    # `hangup_disposition` (caller/callee). Used by compute_end_reason to
    # emit caller_hangup vs agent_hangup chips.
    hangup_direction = db.Column(db.String(20), nullable=True)

    # Return-to-queue tracking (Tier 2p). Increments each time an agent
    # bounces this call back to the queue router via the "Return to queue"
    # action. SLA clock is NOT reset (per the 2p spec — caller's wait time
    # is their wait time regardless of how many agents touched them); these
    # fields are purely for analytics + the soft-cap-at-2 forced-escalation
    # check.
    return_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    last_return_reason = db.Column(db.String(50), nullable=True)

    # Relationships
    transcriptions = db.relationship('Transcription', backref='call', lazy='dynamic', cascade='all, delete-orphan')
    webhook_events = db.relationship('WebhookEvent', backref='call', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Call {self.signalwire_call_sid}>'

    @property
    def ai_context_dict(self):
        """Get AI context as a dict."""
        if not self.ai_context:
            return {}
        try:
            return json.loads(self.ai_context)
        except (json.JSONDecodeError, TypeError):
            return {}

    @ai_context_dict.setter
    def ai_context_dict(self, value):
        """Set AI context from a dict."""
        self.ai_context = json.dumps(value) if value else None

    # Urgency timeout in seconds - if assigned but not accepted within this time, mark as urgent
    URGENCY_TIMEOUT_SECONDS = 30

    @property
    def is_urgent(self):
        """Check if call needs urgent attention.

        A call is urgent if:
        - Status is 'assigned' and agent hasn't accepted within URGENCY_TIMEOUT_SECONDS
        - Or status is explicitly 'urgent'
        """
        if self.status == 'urgent':
            return True
        if self.status == 'assigned' and self.assigned_at:
            elapsed = (datetime.utcnow() - self.assigned_at).total_seconds()
            return elapsed > self.URGENCY_TIMEOUT_SECONDS
        return False

    @property
    def wait_time_seconds(self):
        """Calculate how long the caller has been waiting."""
        if not self.created_at:
            return 0
        return int((datetime.utcnow() - self.created_at).total_seconds())

    @property
    def queue_status(self):
        """Get the effective queue status (accounts for urgency timeout)."""
        if self.status in ('waiting', 'assigned'):
            if self.is_urgent:
                return 'urgent'
        return self.status

    def to_dict(self, include_contact=False):
        """Convert call to dictionary."""
        data = {
            'id': self.id,
            'userId': self.user_id,
            'contactId': self.contact_id,
            'signalwireCallSid': self.signalwire_call_sid,
            'fromNumber': self.from_number,
            'destination': self.destination,
            'destinationType': self.destination_type,
            'direction': self.direction,
            'handlerType': self.handler_type,
            'aiAgentName': self.ai_agent_name,
            'status': self.status,
            'queue_status': self.queue_status,  # Effective status with urgency
            'is_urgent': self.is_urgent,
            'transcriptionActive': self.transcription_active,
            'recordingUrl': self.recording_url,
            'summary': self.summary,
            'duration': self.duration,
            'estimatedCost': _estimated_cost(self),
            'sentimentScore': self.sentiment_score,
            'aiContext': self.ai_context_dict,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'answeredAt': self.answered_at.isoformat() if self.answered_at else None,
            'endedAt': self.ended_at.isoformat() if self.ended_at else None,
            # Queue tracking
            'queue_id': self.queue_id,
            'assigned_agent_id': self.assigned_agent_id,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
            'conference_name': self.conference_name,
            'transport': self.transport or 'conference',
            # Capability set for this call's transport. Frontend uses it to
            # gate which control-panel buttons render (M2 of CALL_TRANSPORT.md).
            # Source of truth lives in app.services.call_transport.<impl>.capabilities;
            # shipping it on every Call payload means a new capability lights
            # up in the UI as soon as the backend declares it.
            'capabilities': sorted(_call_capabilities(self)),
            'wait_time_seconds': self.wait_time_seconds,
            'caller_language': self.caller_language,
            'needs_translation': self.needs_translation,
            # Wrap-up
            'dispositionCode': self.disposition_code,
            'agentNotes': self.agent_notes,
            'wrappedUpAt': self.wrapped_up_at.isoformat() if self.wrapped_up_at else None,
            'wrapUpSource': self.wrap_up_source,
            # Technical ending classification (how it ended)
            'endReason': self.end_reason,
            'hangupDirection': self.hangup_direction,
            # Return-to-queue counters (Tier 2p). UI uses returnCount > 1
            # as a supervisor-review flag, and the soft-cap-at-2 logic in
            # the return endpoint uses it to force escalation.
            'returnCount': self.return_count or 0,
            'lastReturnReason': self.last_return_reason,
        }

        if include_contact and self.contact:
            data['contact'] = self.contact.to_dict_minimal()

        return data

    @classmethod
    def find_by_sid(cls, call_sid):
        """Find call by SignalWire call_id (despite the method name, we search by call_id not SID)."""
        return db.session.query(cls).filter_by(signalwire_call_sid=call_sid).first()

    @classmethod
    def find_by_user(cls, user_id):
        """Find all calls for a user."""
        return db.session.query(cls).filter_by(user_id=user_id).order_by(cls.created_at.desc()).all()

    def compute_end_reason(self):
        """Deterministic classification of HOW this call ended.

        Pure read of the call's own fields — safe to call repeatedly. Returns
        one of the end_reason codes documented on the column.
        """
        if self.status == 'failed':
            return 'failed'

        answered = self.answered_at is not None
        had_agent = self.assigned_agent_id is not None
        duration = self.duration or 0

        if not answered and duration == 0:
            # Never carried audio. Was an agent already on the hook (missed
            # pickup) or did the caller bail before anyone was assigned?
            return 'missed' if had_agent else 'abandoned_in_queue'

        if duration and duration < 10:
            return 'premature_disconnect'

        # Connected and ran a normal length — refine by who hung up if we
        # know. Falls back to 'completed' when direction is unknown.
        if had_agent and self.hangup_direction == 'agent':
            return 'agent_hangup'
        if self.hangup_direction == 'caller':
            return 'caller_hangup'
        return 'completed'

    # Terminal statuses — any of these means the call is over and gets
    # end_reason stamped. Centralized so the webhook / watchdog / agent-end
    # paths all behave the same way.
    TERMINAL_STATUSES = ('ended', 'completed', 'failed')

    def update_status(self, status, end_reason=None):
        """Update call status and set timestamps.

        On transition to a terminal status (ended/completed/failed), also
        stamps end_reason (the technical ending classification) and seals
        ended_at + duration. Callers that already know the reason — e.g.
        the enter_queue status webhook seeing a 'timeout' — can pass it in;
        otherwise it's computed from the call's fields.
        """
        self.status = status
        if status == 'answered' and not self.answered_at:
            self.answered_at = datetime.utcnow()
        elif status in self.TERMINAL_STATUSES and not self.ended_at:
            self.ended_at = datetime.utcnow()
            if self.answered_at:
                delta = self.ended_at - self.answered_at
                self.duration = int(delta.total_seconds())
        if status in self.TERMINAL_STATUSES and not self.end_reason:
            self.end_reason = end_reason or self.compute_end_reason()