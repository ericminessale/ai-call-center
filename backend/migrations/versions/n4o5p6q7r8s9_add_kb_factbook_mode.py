"""Add kb_factbook_mode to users for Agent Assist Factbook setting

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-05-12 19:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'n4o5p6q7r8s9'
down_revision = 'm3n4o5p6q7r8'
branch_labels = None
depends_on = None


def upgrade():
    # Per-user Knowledge Factbook mode for Agent Assist. See AGENT_ASSIST.md.
    #   off    — Factbook panel hidden entirely
    #   manual — typed query + "From transcript" button (default)
    #   auto   — streaming KB facts on every customer turn-end (M4)
    op.add_column(
        'users',
        sa.Column(
            'kb_factbook_mode',
            sa.String(20),
            nullable=False,
            server_default='manual',
        ),
    )


def downgrade():
    op.drop_column('users', 'kb_factbook_mode')
