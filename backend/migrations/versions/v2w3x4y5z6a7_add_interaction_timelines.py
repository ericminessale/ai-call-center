"""Add queue-attempt and handling-segment timelines.

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-07-18 12:00:00.000000

The migration is intentionally additive and does not backfill ambiguous legacy
timestamps. Metrics can prefer these rows when present and retain their legacy
fallback for calls created before this revision.
"""

from alembic import op
import sqlalchemy as sa


revision = 'v2w3x4y5z6a7'
down_revision = 'u1v2w3x4y5z6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'queue_attempts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=False),
        sa.Column('queue_id', sa.Integer(), nullable=True),
        sa.Column('queue_slug', sa.String(length=50), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=True),
        sa.Column('routing_strategy', sa.String(length=30), nullable=True),
        sa.Column('transport', sa.String(length=20), nullable=True),
        sa.Column('entered_at', sa.DateTime(), nullable=False),
        sa.Column('service_started_at', sa.DateTime(), nullable=False),
        sa.Column('first_offered_at', sa.DateTime(), nullable=True),
        sa.Column('last_offered_at', sa.DateTime(), nullable=True),
        sa.Column('offer_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_offered_agent_id', sa.Integer(), nullable=True),
        sa.Column('last_declined_at', sa.DateTime(), nullable=True),
        sa.Column('declined_offer_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_declined_agent_id', sa.Integer(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.Column('accepted_agent_id', sa.Integer(), nullable=True),
        sa.Column('exited_at', sa.DateTime(), nullable=True),
        sa.Column('exit_reason', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['accepted_agent_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_declined_agent_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['last_offered_agent_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['queue_id'], ['queues.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('call_id', 'attempt_number', name='uq_queue_attempt_call_number'),
    )
    op.create_index('ix_queue_attempts_workspace_id', 'queue_attempts', ['workspace_id'])
    op.create_index('ix_queue_attempts_call_id', 'queue_attempts', ['call_id'])
    op.create_index('ix_queue_attempts_queue_id', 'queue_attempts', ['queue_id'])
    op.create_index(
        'ix_queue_attempt_ws_queue_entered', 'queue_attempts',
        ['workspace_id', 'queue_id', 'entered_at'],
    )
    op.create_index(
        'ix_queue_attempt_ws_slug_accepted', 'queue_attempts',
        ['workspace_id', 'queue_slug', 'accepted_at'],
    )
    op.create_index(
        'ix_queue_attempt_call_exited', 'queue_attempts', ['call_id', 'exited_at'],
    )

    op.create_table(
        'handling_segments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=False),
        sa.Column('queue_attempt_id', sa.Integer(), nullable=True),
        sa.Column('segment_type', sa.String(length=20), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=True),
        sa.Column('ai_agent_name', sa.String(length=100), nullable=True),
        sa.Column('transport', sa.String(length=20), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('end_reason', sa.String(length=50), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['queue_attempt_id'], ['queue_attempts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_handling_segments_workspace_id', 'handling_segments', ['workspace_id'])
    op.create_index('ix_handling_segments_call_id', 'handling_segments', ['call_id'])
    op.create_index('ix_handling_segments_queue_attempt_id', 'handling_segments', ['queue_attempt_id'])
    op.create_index('ix_handling_segments_agent_id', 'handling_segments', ['agent_id'])
    op.create_index(
        'ix_handling_segment_call_type_start', 'handling_segments',
        ['call_id', 'segment_type', 'started_at'],
    )
    op.create_index(
        'ix_handling_segment_ws_agent_start', 'handling_segments',
        ['workspace_id', 'agent_id', 'started_at'],
    )
    op.create_index(
        'ix_handling_segment_ws_agent_ended', 'handling_segments',
        ['workspace_id', 'agent_id', 'ended_at'],
    )


def downgrade():
    op.drop_index('ix_handling_segment_ws_agent_ended', table_name='handling_segments')
    op.drop_index('ix_handling_segment_ws_agent_start', table_name='handling_segments')
    op.drop_index('ix_handling_segment_call_type_start', table_name='handling_segments')
    op.drop_index('ix_handling_segments_agent_id', table_name='handling_segments')
    op.drop_index('ix_handling_segments_queue_attempt_id', table_name='handling_segments')
    op.drop_index('ix_handling_segments_call_id', table_name='handling_segments')
    op.drop_index('ix_handling_segments_workspace_id', table_name='handling_segments')
    op.drop_table('handling_segments')

    op.drop_index('ix_queue_attempt_call_exited', table_name='queue_attempts')
    op.drop_index('ix_queue_attempt_ws_slug_accepted', table_name='queue_attempts')
    op.drop_index('ix_queue_attempt_ws_queue_entered', table_name='queue_attempts')
    op.drop_index('ix_queue_attempts_queue_id', table_name='queue_attempts')
    op.drop_index('ix_queue_attempts_call_id', table_name='queue_attempts')
    op.drop_index('ix_queue_attempts_workspace_id', table_name='queue_attempts')
    op.drop_table('queue_attempts')
