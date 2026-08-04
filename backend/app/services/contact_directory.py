"""The single way to resolve a Contact from a phone number.

Two paths insert contacts for the same number: the inbound-call webhook
(``api/swml.py``) and hosted-demo pairing (``services/demo_verify``). Each used
to run its own query-then-insert against its own spelling, which broke in two
directions:

  * They agreed only by coincidence. Pairing stored a normalized ``+digits``
    string; the call path stored SignalWire's raw ``from_number``. Identical
    for well-formed E.164, divergent for any alternate spelling — and because
    the strings differ, ``uq_contacts_workspace_phone`` cheerfully allows BOTH
    rows. A silent duplicate, not an error.
  * Whichever path lost a race hit that constraint for real. The seed commits
    right after pairing publishes the Redis binding, and the binding is what
    lets an inbound call through the verify-first gate — so the call webhook
    can be querying while the seed commits. Losing there raised inside a live
    call's SWML request.

So: one key (``normalize_phone``), one insert strategy, one place to fix.

The create is wrapped in a SAVEPOINT rather than guarded by a pre-check.
Query-then-insert cannot be made safe by checking harder — the gap between the
SELECT and the INSERT is the bug. Letting the unique constraint arbitrate and
re-reading the winner's row is the only version that holds under concurrency,
and the savepoint keeps a conflict from poisoning the caller's transaction
(``api/swml.py`` has an unflushed system-user insert pending at this point and
is nowhere near its commit).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.exc import IntegrityError

from app.utils.phone import normalize_phone, phone_spellings

logger = logging.getLogger(__name__)


def find_contact(workspace_id, raw_number: Optional[str]):
    """Existing Contact for this number in this workspace, or None.

    Matches any legitimate spelling (see :func:`phone_spellings`) so rows
    written before normalization are still found.
    """
    from app.models import Contact

    spellings = phone_spellings(raw_number)
    if not spellings:
        return None
    query = Contact.query.filter(Contact.phone.in_(spellings))
    if workspace_id is not None:
        query = query.filter(Contact.workspace_id == workspace_id)
    return query.first()


def resolve_contact(
    workspace_id,
    raw_number: Optional[str],
    *,
    display_name: Optional[str] = None,
    account_tier: str = 'free',
    account_status: str = 'prospect',
):
    """Get-or-create the Contact for ``raw_number``. None if unusable.

    Stores the CANONICAL spelling, so every row created from here on is
    mutually findable regardless of which path created it. Does not commit —
    the caller owns its transaction boundary.
    """
    from app import db
    from app.models import Contact

    phone = normalize_phone(raw_number)
    if not phone:
        return None

    existing = find_contact(workspace_id, raw_number)
    if existing is not None:
        return existing

    contact = Contact(
        workspace_id=workspace_id,
        phone=phone,
        display_name=display_name or phone,
        account_tier=account_tier,
        account_status=account_status,
    )
    try:
        with db.session.begin_nested():
            db.session.add(contact)
            db.session.flush()
        return contact
    except IntegrityError:
        # Someone inserted the same number between our SELECT and this flush.
        # The SAVEPOINT rolled back, so the caller's transaction is untouched;
        # drop our losing object and use the row that won.
        try:
            db.session.expunge(contact)
        except Exception:  # noqa: BLE001 — already detached is fine
            pass
        winner = find_contact(workspace_id, raw_number)
        if winner is None:
            # Constraint fired but nothing is visible: a genuinely different
            # conflict, not the race. Don't paper over it.
            logger.error(
                "resolve_contact: unique conflict on %s (ws=%s) but no row found",
                phone, workspace_id,
            )
        return winner
