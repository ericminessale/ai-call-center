"""Add coach_mode + coach_intensity to users for Agent Assist AI Coach

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-05-13 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None


def upgrade():
    # Per-user AI Coach mode for Agent Assist Feature 2. See AGENT_ASSIST.md.
    #   off         — no sidecar attached (no billing, no banner, no suggestions)
    #   on_request  — sidecar attached but prompt defaults to sidecar_skip;
    #                 agent triggers suggestions via the "ask coach" button
    #   auto        — sidecar suggests on every customer turn
    op.add_column(
        'users',
        sa.Column(
            'coach_mode',
            sa.String(20),
            nullable=False,
            server_default='off',
        ),
    )

    # Prompt-tone preset fed into the sidecar's system prompt at attach time.
    # Drives the "be terse / standard / verbose" axis distinct from coach_mode.
    op.add_column(
        'users',
        sa.Column(
            'coach_intensity',
            sa.String(20),
            nullable=False,
            server_default='standard',
        ),
    )


def downgrade():
    op.drop_column('users', 'coach_intensity')
    op.drop_column('users', 'coach_mode')
