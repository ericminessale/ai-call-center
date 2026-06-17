"""Add return_count + last_return_reason to calls — Return-caller-to-queue (Tier 2p).

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-06-01 14:50:00.000000

Tracks how many times this call has been bounced back to queue routing by
an agent (vs. transferred to a specific target or completed/hung up).
Supports the soft-cap of 2 returns before forced supervisor escalation,
and gives supervisors an analytics signal — return_count > 1 on a single
call flags for review.

Columns:
- return_count: integer >= 0, defaults to 0
- last_return_reason: string code (wrong-queue / taking-break / cannot-resolve /
                     caller-request / other)

Per the 2p spec, ai_context stays preserved on the Call row across returns
so the next agent picks up with the same collected context.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's9t0u1v2w3x4'
down_revision = 'r8s9t0u1v2w3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'calls',
        sa.Column('return_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'calls',
        sa.Column('last_return_reason', sa.String(50), nullable=True),
    )


def downgrade():
    op.drop_column('calls', 'last_return_reason')
    op.drop_column('calls', 'return_count')
