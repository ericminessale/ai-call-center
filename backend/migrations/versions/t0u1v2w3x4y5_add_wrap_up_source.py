"""Add wrap_up_source to calls — explicit "Captured by AI" provenance.

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-06-22 14:10:00.000000

Records WHO authored the current wrap-up values:
- 'ai'    : auto-filled from the post-prompt report (webhooks.py prefill)
- 'agent' : a human edited/saved the wrap-up (calls.py PUT /wrap-up)
- NULL    : no wrap-up content yet

This replaces the old "wrapped_up_at IS NULL" inference behind the
"Captured by AI" badge with an explicit flag set at the point of write,
where provenance is known with certainty.

Backfill keeps the badge correct on historical rows immediately:
- rows a human finalized (wrapped_up_at IS NOT NULL) -> 'agent'
- remaining rows that already carry AI-filled content (disposition or notes
  present, never human-saved) -> 'ai'
- everything else stays NULL
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't0u1v2w3x4y5'
down_revision = 's9t0u1v2w3x4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('calls', sa.Column('wrap_up_source', sa.String(10), nullable=True))

    # Human-finalized wrap-ups first (wrapped_up_at is only stamped on a human save).
    op.execute(
        """
        UPDATE calls
           SET wrap_up_source = 'agent'
         WHERE wrapped_up_at IS NOT NULL
        """
    )
    # Remaining rows with AI-filled content that no human ever saved.
    op.execute(
        """
        UPDATE calls
           SET wrap_up_source = 'ai'
         WHERE wrap_up_source IS NULL
           AND (disposition_code IS NOT NULL
                OR (agent_notes IS NOT NULL AND agent_notes <> ''))
        """
    )


def downgrade():
    op.drop_column('calls', 'wrap_up_source')
