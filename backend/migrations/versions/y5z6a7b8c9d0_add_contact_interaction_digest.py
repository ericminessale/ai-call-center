"""Add contacts.interaction_digest — the rolling caller-memory summary.

Revision ID: y5z6a7b8c9d0
Revises: x4y5z6a7b8c9

R4 (CONTEXT_AUDIT_2026-08-04): one compact, token-bounded record of the
caller's recent interactions, regenerated from Call rows at call end and
consumed by three surfaces that previously each re-derived (or skipped) it:

  * /api/internal/call-context — inbound AI agents' "Known Caller" context
  * the agent desktop ring-time banner / contact views
  * the chat kernel, when it arrives (channel-agnostic by construction)

JSON array, newest first, at most 3 entries of
``{ended_at, handler, ai_agent, reason, disposition, summary}`` with the
summary clamped — a *digest*, deliberately not a transcript. No backfill:
the column populates as calls end; readers fall back to deriving from Call
rows while it is NULL, so historical contacts lose nothing.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'y5z6a7b8c9d0'
down_revision = 'x4y5z6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'contacts',
        sa.Column('interaction_digest', sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column('contacts', 'interaction_digest')
