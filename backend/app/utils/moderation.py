"""
Content moderation for visitor-typed text in the hosted demo.

The demo's threat model: a public visitor can type into Contact
fields (name, company, notes), inject text into AI messages, etc.
Whatever they type sits in the DB until the nightly reset and is
visible to the next visitor — including, potentially, a real
prospect on a sales call. Slurs, harassment, doxxing, copyright
content all need to be kept out.

Two-tier strategy:

  1. **OpenAI moderation API** (free, multilingual, semantic) when
     ``OPENAI_API_KEY`` is set in the env. Catches harassment, hate
     speech, sexual content, self-harm, violence, etc. — the cases
     a regex blocklist misses.
  2. **Local blocklist fallback** when no API key — basic profanity
     + common slurs. Imperfect but better than nothing.

Both layers are wrapped in :func:`is_text_acceptable` which returns
``(ok, reason)``. Callers get a single boolean to gate the write
plus a short reason string for the user-visible error toast.

The whole module is gated on ``DEMO_MODE`` at the call site —
production-shape clone-and-own deployments don't moderate by default
(operators can opt in if they want, but the canonical path is
unchanged).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# Tight blocklist used as the offline fallback. Word boundaries on
# both sides so 'class' doesn't match 'ass'. Lowercased before match.
# Not exhaustive — for serious moderation, set OPENAI_API_KEY.
_FALLBACK_BLOCKLIST = (
    # generic profanity (sample, lowercase)
    'fuck', 'shit', 'bitch', 'cunt', 'cock', 'dick', 'pussy', 'asshole',
    'bastard', 'motherfucker', 'whore', 'slut',
    # slurs (a small canonical set; real moderation APIs do this far better)
    'nigger', 'nigga', 'faggot', 'tranny', 'retard', 'kike', 'spic', 'chink',
    'gook', 'wetback', 'beaner', 'kraut',
)

_FALLBACK_REGEX = re.compile(
    r'\b(' + '|'.join(re.escape(w) for w in _FALLBACK_BLOCKLIST) + r')\b',
    re.IGNORECASE,
)


def is_text_acceptable(text: Optional[str]) -> tuple[bool, str]:
    """Return ``(ok, reason)``.

    ``ok=True`` means the text passed every layer of moderation; the
    reason will be the empty string. ``ok=False`` means a layer
    flagged it; ``reason`` is a short human-readable explanation
    suitable for a user-visible toast (no PII, no debugging detail).

    Empty / whitespace-only input is always acceptable — let the
    surrounding validation handle "this field can't be blank."
    """
    if not text or not text.strip():
        return True, ''

    # Layer 1: OpenAI moderation when configured.
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if api_key:
        try:
            ok, reason = _check_openai(text, api_key)
            if not ok:
                return False, reason
        except Exception as exc:
            # Don't fail closed if the API has a hiccup — fall through
            # to the local blocklist. We're a demo, not a court of
            # law; a missed flag from a transient outage is fine.
            logger.warning("moderation: OpenAI check errored, falling back: %s", exc)

    # Layer 2: local blocklist (always runs as a baseline).
    match = _FALLBACK_REGEX.search(text)
    if match:
        return False, 'Your input contains language we don\'t allow in this demo.'

    return True, ''


def _check_openai(text: str, api_key: str) -> tuple[bool, str]:
    """Hit OpenAI's free moderation endpoint and return (ok, reason).

    The endpoint returns a per-category score plus a top-level
    ``flagged`` bool. We trust the bool — if OpenAI says it crosses
    the line in any category, we reject.
    """
    resp = requests.post(
        'https://api.openai.com/v1/moderations',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={'model': 'omni-moderation-latest', 'input': text},
        timeout=5,
    )
    resp.raise_for_status()
    body = resp.json()
    # Response shape: {results: [{flagged: bool, categories: {...}, ...}]}
    results = body.get('results') or []
    if not results:
        return True, ''
    result = results[0]
    if not result.get('flagged'):
        return True, ''
    # Categorical reason that doesn't leak the input back.
    cats = result.get('categories') or {}
    flagged_cats = sorted(c for c, v in cats.items() if v)
    label = ', '.join(flagged_cats) if flagged_cats else 'policy'
    return False, f'Your input was flagged ({label}). Please rephrase.'
