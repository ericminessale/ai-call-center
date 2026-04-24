"""Add permissions JSON column to User for fine-grained capability overrides.

Role defaults are applied in code (app/models/user.py); this column only
stores explicit overrides (true to grant beyond the role default, false
to revoke below it). Null / empty = pure role defaults.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-04-24 14:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'users',
        sa.Column(
            'permissions',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )


def downgrade():
    op.drop_column('users', 'permissions')
