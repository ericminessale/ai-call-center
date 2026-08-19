"""Add queues.language_fallback_policy / language_wait_seconds.

Revision ID: a7b8c9d0e1f2
Revises: z6a7b8c9d0e1

What happens when a caller speaks a language none of the available agents
does. Routing already prefers a matching agent and falls back to anyone,
flagging the call for live translation — but "connect them now through
translation" is only one reasonable answer, and which one is right is a
property of the business, not of the code:

  translate_now        connect immediately to any agent + translate
  wait_then_translate  hold for a matching agent up to language_wait_seconds,
                       then connect + translate  (default)
  wait_only            hold for a matching agent; the queue's existing hold
                       cap still applies, so they end up on the callback list
                       rather than waiting forever
  ask_caller           offer the choice on a hold cycle (not implemented yet —
                       needs DTMF collection on a parked leg)

Default is wait_then_translate at 60s rather than translate_now: a real
speaker is better when one is coming soon, and a minute is short enough not
to strand anyone. Existing rows get that default, which is a behaviour change
from today's implicit translate_now — deliberately, since immediately giving
up on a language match is the least defensible of the three.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = 'z6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('queues') as batch:
        batch.add_column(sa.Column(
            'language_fallback_policy', sa.String(30),
            nullable=False, server_default='wait_then_translate',
        ))
        batch.add_column(sa.Column(
            'language_wait_seconds', sa.Integer(),
            nullable=False, server_default='60',
        ))


def downgrade():
    with op.batch_alter_table('queues') as batch:
        batch.drop_column('language_wait_seconds')
        batch.drop_column('language_fallback_policy')
