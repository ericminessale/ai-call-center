from datetime import datetime
from app import db


class DocumentCollection(db.Model):
    """A collection of documents for RAG knowledge bases."""

    __tablename__ = 'document_collections'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = db.relationship('Document', backref='collection', cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_doc_count=True):
        result = {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_doc_count:
            result['document_count'] = self.documents.count()
        return result


class Document(db.Model):
    """A document within a collection, containing knowledge base content."""

    __tablename__ = 'documents'

    id = db.Column(db.Integer, primary_key=True)
    collection_id = db.Column(db.Integer, db.ForeignKey('document_collections.id'), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_published = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'collection_id': self.collection_id,
            'title': self.title,
            'content': self.content,
            'is_published': self.is_published,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AgentCollectionAssignment(db.Model):
    """Maps AI agents to their assigned document collections."""

    __tablename__ = 'agent_collection_assignments'

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.String(50), nullable=False)
    collection_id = db.Column(db.Integer, db.ForeignKey('document_collections.id'), nullable=False)

    collection = db.relationship('DocumentCollection')

    __table_args__ = (db.UniqueConstraint('agent_id', 'collection_id'),)

    def to_dict(self):
        return {
            'id': self.id,
            'agent_id': self.agent_id,
            'collection_id': self.collection_id,
            'collection_name': self.collection.name if self.collection else None,
            'collection_display_name': self.collection.display_name if self.collection else None,
        }
