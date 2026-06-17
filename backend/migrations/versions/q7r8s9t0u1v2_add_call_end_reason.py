"""Add end_reason to calls — deterministic technical ending classification

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-05-22 12:35:00.000000

end_reason captures HOW a call ended (abandoned_in_queue, missed,
premature_disconnect, completed, failed) — distinct from disposition_code
which is the agent's BUSINESS outcome (resolved, sales-opportunity, etc.).
Computed deterministically on call end; surfaced as the call-history status
chip. See Call.compute_end_reason().
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'q7r8s9t0u1v2'
down_revision = 'p6q7r8s9t0u1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'calls',
        sa.Column('end_reason', sa.String(40), nullable=True),
    )


def downgrade():
    op.drop_column('calls', 'end_reason')
