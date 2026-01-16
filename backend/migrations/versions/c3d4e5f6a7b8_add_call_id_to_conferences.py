"""Add call_id column to conferences table for per-interaction conferences.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2025-01-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    """Add call_id column to conferences table.

    This column tracks which call an interaction conference was created for.
    Used in the per-interaction conference model where each customer call
    gets its own conference (instead of agents sitting in persistent conferences).
    """
    # Add call_id column (nullable since existing conferences won't have it)
    op.add_column('conferences', sa.Column('call_id', sa.String(255), nullable=True))

    # Add index for faster lookups by call_id
    op.create_index('ix_conferences_call_id', 'conferences', ['call_id'])


def downgrade():
    """Remove call_id column from conferences table."""
    op.drop_index('ix_conferences_call_id', table_name='conferences')
    op.drop_column('conferences', 'call_id')
