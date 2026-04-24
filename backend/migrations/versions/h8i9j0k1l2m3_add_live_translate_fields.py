"""Add live_translate fields: user languages, call needs_translation + caller_language

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-04-24 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    # User language profile — list of BCP-47 codes (e.g. ["en-US", "es-ES"])
    op.add_column(
        'users',
        sa.Column(
            'languages',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[\"en-US\"]'::json"),
        ),
    )

    # Call translation flags — set during routing, used at conference-join
    op.add_column(
        'calls',
        sa.Column('caller_language', sa.String(20), nullable=True),
    )
    op.add_column(
        'calls',
        sa.Column(
            'needs_translation',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade():
    op.drop_column('calls', 'needs_translation')
    op.drop_column('calls', 'caller_language')
    op.drop_column('users', 'languages')
