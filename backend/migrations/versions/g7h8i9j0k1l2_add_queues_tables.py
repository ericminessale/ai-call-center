"""Add queues and queue_agent_assignments tables

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g7h8i9j0k1l2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('queues',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('slug', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('routing_strategy', sa.String(30), server_default='round_robin', nullable=False),
        sa.Column('ai_agent_route', sa.String(100), nullable=True),
        sa.Column('default_priority', sa.Integer(), server_default='5'),
        sa.Column('sla_threshold_seconds', sa.Integer(), server_default='60'),
        sa.Column('max_wait_before_ai_fallback', sa.Integer(), server_default='120'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_queues_slug', 'queues', ['slug'])

    op.create_table('queue_agent_assignments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('queue_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('skill_level', sa.Integer(), server_default='5'),
        sa.Column('is_activated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['queue_id'], ['queues.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('queue_id', 'user_id'),
    )

    # Seed default queues matching currently hardcoded ones
    op.execute("""
        INSERT INTO queues (slug, display_name, description, routing_strategy, ai_agent_route)
        VALUES
            ('sales', 'Sales', 'Sales inquiries and purchases', 'round_robin', '/sales-ai'),
            ('support', 'Support', 'Technical support and troubleshooting', 'priority', '/support-ai'),
            ('billing', 'Billing', 'Billing questions and account issues', 'round_robin', '/support-ai')
    """)


def downgrade():
    op.drop_table('queue_agent_assignments')
    op.drop_index('ix_queues_slug', 'queues')
    op.drop_table('queues')
