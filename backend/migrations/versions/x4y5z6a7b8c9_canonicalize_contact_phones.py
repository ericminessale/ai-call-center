"""Rewrite contacts.phone to the canonical spelling.

Revision ID: x4y5z6a7b8c9
Revises: w3x4y5z6a7b8

``contacts.phone`` is a KEY — every path that resolves a caller to a contact
matches on it — but it was written by several code paths with several
spellings:

  * ``api/swml.py`` and ``api/queues.py`` stored SignalWire's raw
    ``from_number`` verbatim.
  * ``api/contacts.py`` stored ``Contact.normalize_phone(...)``, whose old
    implementation returned BARE digits (no ``+``) for anything under 10
    digits without a leading plus, and an empty string when the input had no
    digits at all.
  * hosted-demo pairing stored a strict ``+digits``.

Because the strings differ, ``uq_contacts_workspace_phone`` does not fire —
the same human phone number could occupy several rows, and a lookup keyed on
one spelling silently created yet another. Normalizing the writers only fixes
new rows; existing ones stay unfindable until they are rewritten here.

Canonical form (``app.utils.phone.normalize_phone``): ``+`` followed by digits
only, minimum 7 digits.

Rows deliberately left alone:
  * phone that cannot be normalized (junk / too short) — there is nothing
    better to write, and rewriting to NULL would break a NOT NULL column.
  * a row whose canonical form is ALREADY taken by another row in the same
    workspace. That pair is a pre-existing duplicate; rewriting would trip the
    unique constraint and abort the whole migration. Merging them means
    choosing which call history and which notes survive, which is a product
    decision, not a schema one. Leaving them is exactly the status quo, and
    they are reported in the migration log so they can be merged by hand.
"""
import logging
import re

import sqlalchemy as sa
from alembic import op

revision = 'x4y5z6a7b8c9'
down_revision = 'w3x4y5z6a7b8'
branch_labels = None
depends_on = None

logger = logging.getLogger('alembic.runtime.migration')

_MIN_DIGITS = 7


def _canonical(value):
    """Mirror of app.utils.phone.normalize_phone.

    Deliberately duplicated rather than imported: a migration must keep
    behaving the way it did the day it ran, even if the app's helper is
    retuned later.
    """
    if not value:
        return None
    digits = re.sub(r'[^0-9]', '', str(value))
    if len(digits) < _MIN_DIGITS:
        return None
    return '+' + digits


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(
        sa.text('SELECT id, workspace_id, phone FROM contacts')
    ).fetchall()

    # (workspace_id, phone) already present, so we can detect a collision
    # before attempting the UPDATE rather than catching a constraint error
    # mid-transaction.
    taken = {(r[1], r[2]) for r in rows}

    rewritten = 0
    skipped_unusable = 0
    skipped_collision = []

    for row_id, ws_id, phone in rows:
        canon = _canonical(phone)
        if canon is None:
            skipped_unusable += 1
            continue
        if canon == phone:
            continue
        if (ws_id, canon) in taken:
            skipped_collision.append((row_id, ws_id, phone, canon))
            continue
        bind.execute(
            sa.text('UPDATE contacts SET phone = :new WHERE id = :id'),
            {'new': canon, 'id': row_id},
        )
        taken.discard((ws_id, phone))
        taken.add((ws_id, canon))
        rewritten += 1

    logger.info(
        'canonicalize_contact_phones: rewrote %d, left %d unusable, '
        '%d collisions', rewritten, skipped_unusable, len(skipped_collision),
    )
    for row_id, ws_id, old, canon in skipped_collision:
        logger.warning(
            'canonicalize_contact_phones: contact id=%s (workspace %s) keeps '
            'spelling %r — %r is already taken in that workspace. Duplicate '
            'pair, merge by hand.', row_id, ws_id, old, canon,
        )


def downgrade():
    # Irreversible by nature: the original spellings are not recoverable from
    # the canonical form. A no-op downgrade is honest; raising would block an
    # otherwise-valid rollback of later revisions.
    pass
