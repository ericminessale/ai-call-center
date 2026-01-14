from datetime import datetime
from app import db


class Transcription(db.Model):
    """Transcription model to store call transcriptions."""

    __tablename__ = 'transcriptions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=False)
    transcript = db.Column(db.Text)
    summary = db.Column(db.Text)
    confidence = db.Column(db.Float)
    is_final = db.Column(db.Boolean, default=False)
    sequence_number = db.Column(db.Integer)
    speaker = db.Column(db.String(50))
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
        """Get the complete transcript for a call."""
        transcriptions = db.session.query(cls).filter_by(
            call_id=call_id,
            is_final=True
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