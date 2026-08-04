"""Canonical phone-number spelling.

One representation, used wherever a phone number is a KEY rather than display
text: ``+`` followed by digits only. Callers hand us whatever SignalWire, a
webhook form field, or the frontend gave them; everything that has to MATCH
goes through here first.

Deliberately does not guess country codes or validate. Exact-match semantics
are all the storage layer needs, and inventing a country code would silently
bind two different real numbers together.
"""

from __future__ import annotations

import re
from typing import Optional

# Below this, it can't be a dialable national number and is almost certainly a
# short code, an extension, or junk — better to reject than to key on it.
_MIN_DIGITS = 7


def normalize_phone(number: Optional[str]) -> Optional[str]:
    """``'+1 (262) 555-0199'`` → ``'+12625550199'``. None when unusable."""
    if not number:
        return None
    digits = re.sub(r'[^0-9]', '', str(number))
    if len(digits) < _MIN_DIGITS:
        return None
    return '+' + digits


def phone_spellings(raw: Optional[str]) -> list[str]:
    """Every spelling a stored row might legitimately use for ``raw``.

    The canonical form first, then the caller's original if it differs. Rows
    written before normalization existed hold the raw spelling, so a lookup
    that only searched the canonical form would miss them and insert a
    duplicate.
    """
    norm = normalize_phone(raw)
    out: list[str] = []
    if norm:
        out.append(norm)
    if raw and raw not in out:
        out.append(str(raw))
    return out
