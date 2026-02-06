"""Add admin tables: system_config, document_collections, documents, agent_collection_assignments

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-02-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # Enable pgvector extension
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    # System configuration (key-value store for admin settings)
    op.create_table('system_config',
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.PrimaryKeyConstraint('key')
    )

    # Document collections for RAG knowledge bases
    op.create_table('document_collections',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    # Documents within collections
    op.create_table('documents',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_published', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['collection_id'], ['document_collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Agent-to-collection assignments
    op.create_table('agent_collection_assignments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('agent_id', sa.String(length=50), nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['collection_id'], ['document_collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'collection_id')
    )

    # Seed default routing config
    op.execute("""
        INSERT INTO system_config (key, value) VALUES
            ('route.initial_handler', '/receptionist'),
            ('route.sales_specialist', '/sales-ai'),
            ('route.support_specialist', '/support-ai')
        ON CONFLICT (key) DO NOTHING
    """)

    # Seed default document collections
    op.execute("""
        INSERT INTO document_collections (name, display_name, description) VALUES
            ('sales_knowledge', 'Sales Knowledge Base', 'Product info, pricing, sales scripts'),
            ('support_knowledge', 'Support Knowledge Base', 'Troubleshooting guides, FAQs, diagnostics')
        ON CONFLICT (name) DO NOTHING
    """)

    # Seed default agent-collection assignments
    op.execute("""
        INSERT INTO agent_collection_assignments (agent_id, collection_id)
        SELECT agent_id, collection_id FROM (VALUES
            ('sales-ai', 1),
            ('outbound-sales', 1),
            ('support-ai', 2),
            ('outbound-support', 2)
        ) AS defaults(agent_id, collection_id)
        WHERE EXISTS (SELECT 1 FROM document_collections WHERE id = collection_id)
        ON CONFLICT (agent_id, collection_id) DO NOTHING
    """)


def downgrade():
    op.drop_table('agent_collection_assignments')
    op.drop_table('documents')
    op.drop_table('document_collections')
    op.drop_table('system_config')
