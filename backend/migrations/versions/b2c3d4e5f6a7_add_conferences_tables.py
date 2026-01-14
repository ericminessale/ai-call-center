"""Add conferences and conference_participants tables for conference-based routing

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-13 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Create conferences table
    op.create_table('conferences',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conference_name', sa.String(length=255), nullable=False),
        sa.Column('conference_type', sa.String(length=50), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('owner_ai_agent', sa.String(length=100), nullable=True),
        sa.Column('queue_id', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conferences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conferences_conference_name'), ['conference_name'], unique=True)

    # Create conference_participants table
    op.create_table('conference_participants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conference_id', sa.Integer(), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=True),
        sa.Column('participant_type', sa.String(length=50), nullable=False),
        sa.Column('participant_id', sa.String(length=255), nullable=False),
        sa.Column('call_sid', sa.String(length=255), nullable=True),
        sa.Column('direction', sa.String(length=20), nullable=True),  # 'inbound' or 'outbound'
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.Column('left_at', sa.DateTime(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('is_muted', sa.Boolean(), nullable=True),
        sa.Column('is_deaf', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ),
        sa.ForeignKeyConstraint(['conference_id'], ['conferences.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conference_participants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conference_participants_conference_id'), ['conference_id'], unique=False)

    # Add conference columns to call_legs table
    with op.batch_alter_table('call_legs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conference_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('conference_name', sa.String(length=255), nullable=True))
        batch_op.create_foreign_key('fk_call_legs_conference_id', 'conferences', ['conference_id'], ['id'])


def downgrade():
    # Remove conference columns from call_legs table
    with op.batch_alter_table('call_legs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_call_legs_conference_id', type_='foreignkey')
        batch_op.drop_column('conference_name')
        batch_op.drop_column('conference_id')

    # Drop conference_participants table
    with op.batch_alter_table('conference_participants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conference_participants_conference_id'))
    op.drop_table('conference_participants')

    # Drop conferences table
    with op.batch_alter_table('conferences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conferences_conference_name'))
    op.drop_table('conferences')
