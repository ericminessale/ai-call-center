"""Add hangup_direction to calls — who ended the call (caller vs agent)

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-05-22 12:42:00.000000

hangup_direction captures who hung up first so end_reason can distinguish
caller_hangup from agent_hangup (table stakes in any CCaaS). Set from two
sources: (1) SignalWire's call-state 'ended' payload includes
hangup_disposition (caller/callee) which we map to caller/agent; (2) the
frontend signals 'agent' explicitly when the agent presses the hangup
button, which is more authoritative than waiting for the webhook.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'r8s9t0u1v2w3'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'calls',
        sa.Column('hangup_direction', sa.String(20), nullable=True),
    )


def downgrade():
    op.drop_column('calls', 'hangup_direction')
