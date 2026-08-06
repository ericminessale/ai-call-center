"""Add contacts.preferred_language — the caller's documented language.

Revision ID: z6a7b8c9d0e1
Revises: y5z6a7b8c9d0

A contact-level language, distinct from ``calls.caller_language`` (which
records what a SINGLE call was conducted in). This one is durable and
human-settable: an agent who knows a customer speaks Spanish can say so, and
the AI seeds it from a call when the field is still empty. It is never
overwritten by the AI once set, so a human's assertion wins.

Consumed by the caller-memory block: when a returning caller's documented
language is one the agent actually speaks, the AI opens in that language
(with an immediate English offer, since a phone number is not a person).

Backfilled from the most recent call that recorded a non-English
caller_language, so contacts who have already told us once benefit on their
next call instead of waiting for a fresh one.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'z6a7b8c9d0e1'
down_revision = 'y5z6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'contacts',
        sa.Column('preferred_language', sa.String(20), nullable=True),
    )
    # Seed from history: the newest call per contact that carried a
    # non-English language. Same-workspace by construction (calls.contact_id
    # already points within the workspace; the app enforces it on write).
    op.execute(
        """
        UPDATE contacts c
           SET preferred_language = sub.caller_language
          FROM (
                SELECT DISTINCT ON (contact_id)
                       contact_id, caller_language
                  FROM calls
                 WHERE contact_id IS NOT NULL
                   AND caller_language IS NOT NULL
                   AND caller_language <> ''
                   AND caller_language NOT LIKE 'en%%'
                 ORDER BY contact_id, created_at DESC
               ) AS sub
         WHERE c.id = sub.contact_id
           AND c.preferred_language IS NULL
        """
    )


def downgrade():
    op.drop_column('contacts', 'preferred_language')
