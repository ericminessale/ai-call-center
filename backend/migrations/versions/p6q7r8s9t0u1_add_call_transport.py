"""Add transport column to calls + routing_transport to queues for the
call_transport abstraction. See CALL_TRANSPORT.md.

This migration is M0 (foundations, no behavior change): both columns default
to 'conference' so existing call routing is bit-identical. M1 will start
populating 'bridge' for queues that admins opt into bridge mode.

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-05-13 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None


def upgrade():
    # Per-call transport on the Call row. Set once at ingress in
    # build_ingress_swml and never changes for the life of the call (M4 could
    # later support promotion bridge→conference, which would flip this).
    #
    # Default 'conference' so every existing and unchanged-code-path call
    # behaves identically to before.
    op.add_column(
        'calls',
        sa.Column(
            'transport',
            sa.String(20),
            nullable=False,
            server_default='conference',
        ),
    )

    # Per-queue routing preference. Admin-set via Settings → Queues. The
    # actual transport is decided at ingress per call (see Risk 1 in
    # CALL_TRANSPORT.md): if routing_transport='bridge' but no agent is
    # available, ingress falls back to conference parking.
    op.add_column(
        'queues',
        sa.Column(
            'routing_transport',
            sa.String(20),
            nullable=False,
            server_default='conference',
        ),
    )


def downgrade():
    op.drop_column('queues', 'routing_transport')
    op.drop_column('calls', 'transport')
