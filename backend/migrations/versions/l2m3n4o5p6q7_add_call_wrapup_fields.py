"""Add call wrap-up fields: disposition_code + agent_notes

Tier 2a — post-call wrap-up UI. Lets the human agent (or supervisor
post-hoc) tag a call with a disposition code and free-text notes once
the conversation ends. The `summary` and `ai_context` columns continue
to hold AI-generated context; these new columns are explicitly the
human agent's wrap-up record.

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-04-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    # Disposition code — short slug picked by the agent on wrap-up.
    # Stored as a free-form string so the customer can extend the list
    # via system_config without a schema change.
    op.add_column(
        'calls',
        sa.Column('disposition_code', sa.String(50), nullable=True),
    )

    # Agent notes — free-text wrap-up. Distinct from `summary` (AI-generated)
    # and from Contact.notes (durable contact-level notes); these are about
    # this specific interaction.
    op.add_column(
        'calls',
        sa.Column('agent_notes', sa.Text(), nullable=True),
    )

    # When the wrap-up was finalized — useful for SLA reporting and ACW timer.
    op.add_column(
        'calls',
        sa.Column('wrapped_up_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('calls', 'wrapped_up_at')
    op.drop_column('calls', 'agent_notes')
    op.drop_column('calls', 'disposition_code')
