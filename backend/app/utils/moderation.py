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
import unicodedata
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# Tight blocklist used as the offline fallback. Word boundaries on
# both sides so 'class' doesn't match 'ass'. Lowercased + leet-
# normalized before match. Focus is canonical profanity + slurs that
# OpenAI's moderation threshold sometimes lets through under
# obfuscation (e.g. 'pu$$y', 'sh1t', 'f@ck'). Not exhaustive — for
# serious moderation, set OPENAI_API_KEY for the semantic layer.
_FALLBACK_BLOCKLIST = (
    # generic profanity (sample, lowercase)
    'fuck', 'shit', 'bitch', 'cunt', 'cock', 'dick', 'pussy', 'asshole',
    'bastard', 'motherfucker', 'whore', 'slut',
    # slurs (a small canonical set; real moderation APIs do this far better)
    'nigger', 'nigga', 'faggot', 'tranny', 'retard', 'kike', 'spic', 'chink',
    'gook', 'wetback', 'beaner', 'kraut',
)

# Word-boundary match against a leet-normalized haystack. We build
# the pattern dynamically so each vowel position in a blocklist word
# is treated as a tolerant class — matching the canonical vowel,
# common censor characters ('*', '@', '!'), or nothing at all. That
# catches a few obfuscation families at once:
#   - 'f@ck' / 'f*ck'  (vowel replaced by censor mark)
#   - 'fck' / 'sht'    (vowel deleted entirely)
#   - 'fuck' / 'shit'  (canonical spelling)
# Trickier bypasses (zero-width spaces, homoglyphs, per-letter
# spacing like 'p u s s y') aren't handled here — that's the
# OpenAI semantic layer's job.
_VOWEL_CLASS = '[aeiou*@!]?'

def _build_blocklist_regex(words: tuple[str, ...]) -> re.Pattern:
    patterns = []
    for word in words:
        chars = []
        for ch in word.lower():
            if ch in 'aeiou':
                chars.append(_VOWEL_CLASS)
            else:
                chars.append(re.escape(ch))
        patterns.append(''.join(chars))
    return re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.IGNORECASE)


_FALLBACK_REGEX = _build_blocklist_regex(_FALLBACK_BLOCKLIST)

# Common leetspeak substitutions used to obfuscate blocklisted words.
# Mapped 1:1 — covers the most frequent bypasses ('$' for 's', '@'
# for 'a', '0' for 'o', etc.) without trying to be exhaustive.
# Lowercase keys; the input is lowered before translation runs.
_LEET_TABLE = str.maketrans({
    '$': 's',
    '5': 's',
    '@': 'a',
    '4': 'a',
    '0': 'o',
    '1': 'i',  # could equally map to 'l'; 'i' wins for our blocklist
    '3': 'e',
    '7': 't',
    '!': 'i',
    '|': 'i',
})


def _normalize_leet(text: str) -> str:
    """Return the input lowercased and leet-substituted.

    Applied before the local blocklist regex so 'pu$$y' / 'sh1t' /
    'f@ck' get caught alongside their canonical spellings.
    """
    return text.lower().translate(_LEET_TABLE)


# Match runs of two or more single-letter "words" separated by any
# whitespace, e.g. 'p u s s y', 'f u c k', 'k i l l   m e'. The
# replacement collapses them so the blocklist regex can match the
# concatenated form. Limited to runs of 2+ to avoid eating
# legitimate text like 'I a m here' (only matches 2+ in a row).
_LETTER_SPACING_PATTERN = re.compile(
    r'(?:\b[a-z]\b\s+){1,}\b[a-z]\b',  # 'a b c d' style — 2+ singles
    re.IGNORECASE,
)


def _collapse_letter_spacing(text: str) -> str:
    """Mash runs of space-separated single letters back together.

    'p u s s y' → 'pussy'. Used as a normalization layer before the
    blocklist regex check so per-letter-spacing bypasses don't slip
    through.
    """
    def _collapse(match: re.Match) -> str:
        # Strip every whitespace char from the matched run.
        return re.sub(r'\s+', '', match.group(0))
    return _LETTER_SPACING_PATTERN.sub(_collapse, text)


_ZERO_WIDTH_CHARS = re.compile(
    '[​‌‍‎‏‪-‮⁠﻿]'
)


def _normalize_unicode(text: str) -> str:
    """NFKC-normalize + strip zero-width / bidi format characters.

    NFKC handles compatibility decomposition — fullwidth chars
    become ASCII, ligatures like 'ﬁ' decompose to 'fi'. (Note: NFKC
    does NOT map cross-script homoglyphs like Cyrillic 'а' to Latin
    'a' — those have separate codepoints in the same Unicode
    category. Cross-script homoglyph attacks remain a known gap;
    OpenAI catches most of them at the semantic layer.)
    Then strip zero-width + bidi-format characters that visitors
    paste in to break up words invisibly.
    """
    normalized = unicodedata.normalize('NFKC', text)
    return _ZERO_WIDTH_CHARS.sub('', normalized)


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

    # Layer 2: local blocklist. Run the regex against several
    # normalized projections of the input so common bypass families
    # all collapse onto the canonical blocklist words.
    #
    # Pipeline order matters: leet substitution runs BEFORE the
    # letter-spacing collapse so combined bypasses like 'p u $ $ y'
    # become 'p u s s y' (leet) → 'pussy' (collapse). We also keep
    # earlier projections in the check set so isolated bypasses get
    # caught at the right layer.
    unicode_clean = _normalize_unicode(text)
    leet_normalized = _normalize_leet(unicode_clean)
    spacing_collapsed = _collapse_letter_spacing(leet_normalized)
    if (
        _FALLBACK_REGEX.search(text)
        or _FALLBACK_REGEX.search(unicode_clean)
        or _FALLBACK_REGEX.search(leet_normalized)
        or _FALLBACK_REGEX.search(spacing_collapsed)
    ):
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
