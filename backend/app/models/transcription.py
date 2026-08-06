from datetime import datetime
from app import db
from app.tenancy import WorkspaceScoped


class Transcription(WorkspaceScoped, db.Model):
    """Transcription model to store call transcriptions.

    ``speaker`` values:
      - ``'caller'``  — the remote caller (live_transcribe role 'remote-caller')
      - ``'ai'``      — the AI agent, while ``Call.handler_type == 'ai'``
      - ``'agent'``   — the human agent (non-caller side once a human handles
                        the call)
      - ``'system'``  — synthetic marker rows (AI→human handoff divider);
                        nothing was actually said, so these are excluded from
                        :meth:`get_full_transcript`
      - ``NULL``      — legacy rows and summary-only rows (:meth:`save_summary`)

    The non-caller mapping is decided per-utterance from the call's current
    handler because one live_transcribe session spans the AI→human handoff
    (see webhooks._process_utterance_event).
    """

    __tablename__ = 'transcriptions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Tenancy: nullable — derived from the parent call at flush.
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=True,
    )
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=False)
    transcript = db.Column(db.Text)
    summary = db.Column(db.Text)
    confidence = db.Column(db.Float)
    is_final = db.Column(db.Boolean, default=False)
    sequence_number = db.Column(db.Integer)
    speaker = db.Column(db.String(50))  # 'caller' | 'ai' | 'agent' | 'system' | NULL — see class docstring
    language = db.Column(db.String(10), default='en-US')
    keywords = db.Column(db.JSON)  # Store keywords as JSON array
    sentiment = db.Column(db.String(20))  # positive, negative, neutral
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<Transcription {self.id}>'

    def to_dict(self):
        """Convert transcription to dictionary."""
        return {
            'id': self.id,
            'call_id': self.call_id,
            'transcript': self.transcript,
            'summary': self.summary,
            'confidence': self.confidence,
            'is_final': self.is_final,
            'sequence_number': self.sequence_number,
            'speaker': self.speaker,
            'language': self.language,
            'keywords': self.keywords,
            'sentiment': self.sentiment,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    @classmethod
    def find_by_call(cls, call_id):
        """Find all transcriptions for a call."""
        return db.session.query(cls).filter_by(call_id=call_id).order_by(cls.sequence_number.asc()).all()

    @classmethod
    def get_full_transcript(cls, call_id):
        """Get the complete transcript for a call.

        Synthetic 'system' rows (handoff markers) carry text nobody said, so
        they are excluded — this string feeds search and summarization.
        """
        transcriptions = db.session.query(cls).filter_by(
            call_id=call_id,
            is_final=True
        ).filter(
            # speaker != 'system' would also drop legacy NULL-speaker rows
            # (SQL NULL comparison), so keep NULLs explicitly.
            db.or_(cls.speaker.is_(None), cls.speaker != 'system')
        ).order_by(cls.sequence_number.asc()).all()

        return ' '.join([t.transcript for t in transcriptions if t.transcript])

    @classmethod
    def save_summary(cls, call_id, summary_data):
        """Save or update summary for a call."""
        # Find existing transcription or create new one
        transcription = db.session.query(cls).filter_by(call_id=call_id, summary=None).first()

        if not transcription:
            transcription = cls(call_id=call_id)

        transcription.summary = summary_data.get('text')
        transcription.keywords = summary_data.get('keywords', [])
        transcription.sentiment = summary_data.get('sentiment', 'neutral')

        db.session.add(transcription)
        db.session.commit()

        return transcription