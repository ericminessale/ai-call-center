from datetime import datetime
from app import db
from app.tenancy import WorkspaceScoped


class DocumentCollection(WorkspaceScoped, db.Model):
    """A collection of documents for RAG knowledge bases."""

    __tablename__ = 'document_collections'

    id = db.Column(db.Integer, primary_key=True)
    # Tenancy: display identity (`name`) is unique per workspace so every
    # tenant can have "sales_knowledge"; `physical_name` is the globally
    # unique identity for chunk tables / search indexes (ws{ID}_{name} for
    # clones, = name for the default workspace's rows).
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=False,
    )
    name = db.Column(db.String(100), nullable=False)
    physical_name = db.Column(db.String(150), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = db.relationship('Document', backref='collection', cascade='all, delete-orphan', lazy='dynamic')

    __table_args__ = (db.UniqueConstraint('workspace_id', 'name', name='uq_document_collections_workspace_name'),)

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


class Document(WorkspaceScoped, db.Model):
    """A document within a collection, containing knowledge base content."""

    __tablename__ = 'documents'

    id = db.Column(db.Integer, primary_key=True)
    # Tenancy: denormalized from the collection so direct-by-id document
    # endpoints (admin.py /documents/<id>) are auto-scoped without a join.
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=False,
    )
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


class AgentCollectionAssignment(WorkspaceScoped, db.Model):
    """Maps AI agents to their assigned document collections."""

    __tablename__ = 'agent_collection_assignments'

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey('workspaces.id'), nullable=False,
    )
    agent_id = db.Column(db.String(50), nullable=False)
    collection_id = db.Column(db.Integer, db.ForeignKey('document_collections.id'), nullable=False)

    collection = db.relationship('DocumentCollection')

    __table_args__ = (
        db.UniqueConstraint(
            'workspace_id', 'agent_id', 'collection_id',
            name='uq_agent_collection_assignments_workspace',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'agent_id': self.agent_id,
            'collection_id': self.collection_id,
            'collection_name': self.collection.name if self.collection else None,
            'collection_display_name': self.collection.display_name if self.collection else None,
        }
