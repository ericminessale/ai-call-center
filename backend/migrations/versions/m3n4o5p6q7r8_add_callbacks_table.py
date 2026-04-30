"""Add callbacks table — Tier 2r Callback System

Caller-requested callbacks (or agent-scheduled outbound followups). One
row per request; lifecycle tracked via columns rather than separate
status table — see app/models/callback.py for the state machine.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-04-30 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'callbacks',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('call_id', sa.Integer(), nullable=True),
        sa.Column('contact_id', sa.Integer(), nullable=True),
        sa.Column('queue_id', sa.String(50), nullable=True),
        sa.Column('phone_number', sa.String(64), nullable=False),
        sa.Column('caller_name', sa.String(255), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('ai_context', sa.Text(), nullable=True),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('claimed_by_agent_id', sa.Integer(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('outcome', sa.String(32), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['claimed_by_agent_id'], ['users.id'], ondelete='SET NULL'),
    )

    # Indexes — pending list orders by requested_at, dashboards filter by queue / contact.
    op.create_index('ix_callbacks_call_id', 'callbacks', ['call_id'])
    op.create_index('ix_callbacks_contact_id', 'callbacks', ['contact_id'])
    op.create_index('ix_callbacks_queue_id', 'callbacks', ['queue_id'])
    op.create_index('ix_callbacks_requested_at', 'callbacks', ['requested_at'])


def downgrade():
    op.drop_index('ix_callbacks_requested_at', table_name='callbacks')
    op.drop_index('ix_callbacks_queue_id', table_name='callbacks')
    op.drop_index('ix_callbacks_contact_id', table_name='callbacks')
    op.drop_index('ix_callbacks_call_id', table_name='callbacks')
    op.drop_table('callbacks')
