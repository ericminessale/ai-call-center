"""Add queue tracking fields to calls

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-01-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    # Add queue tracking fields to calls table
    with op.batch_alter_table('calls', schema=None) as batch_op:
        # Queue ID - which queue the call is in
        batch_op.add_column(sa.Column('queue_id', sa.String(length=50), nullable=True))
        batch_op.create_index(batch_op.f('ix_calls_queue_id'), ['queue_id'], unique=False)

        # Assigned agent - which agent is assigned to handle this call
        batch_op.add_column(sa.Column('assigned_agent_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_calls_assigned_agent_id', 'users', ['assigned_agent_id'], ['id'])

        # Assigned at - when the agent was notified
        batch_op.add_column(sa.Column('assigned_at', sa.DateTime(), nullable=True))

        # Conference name - the interaction conference for this call
        batch_op.add_column(sa.Column('conference_name', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.drop_column('conference_name')
        batch_op.drop_column('assigned_at')
        batch_op.drop_constraint('fk_calls_assigned_agent_id', type_='foreignkey')
        batch_op.drop_column('assigned_agent_id')
        batch_op.drop_index(batch_op.f('ix_calls_queue_id'))
        batch_op.drop_column('queue_id')
