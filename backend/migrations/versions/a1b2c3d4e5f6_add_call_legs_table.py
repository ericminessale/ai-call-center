"""Add call_legs table for tracking call handler transitions

Revision ID: a1b2c3d4e5f6
Revises: 8de9f530e024
Create Date: 2026-01-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '8de9f530e024'
branch_labels = None
depends_on = None


def upgrade():
    # Create call_legs table
    op.create_table('call_legs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('leg_type', sa.String(length=50), nullable=False),
        sa.Column('leg_number', sa.Integer(), nullable=True),
        sa.Column('ai_agent_name', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('transition_reason', sa.String(length=100), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('call_legs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_call_legs_call_id'), ['call_id'], unique=False)


def downgrade():
    with op.batch_alter_table('call_legs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_call_legs_call_id'))

    op.drop_table('call_legs')
