"""Privacy-safe request metadata for operational webhook logs.

Webhook bodies can contain phone numbers, transcripts, customer context, and
credentials embedded in callback URLs. Production logs should describe the
shape of a request without copying those values into a second data store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


def payload_keys(payload: Any, *, limit: int = 50) -> list[str]:
    """Return stable top-level field names without exposing field values."""
    if not isinstance(payload, Mapping):
        return []
    return sorted(str(key) for key in payload.keys())[:limit]


def request_summary(req: Any, payload: Any = None) -> dict[str, Any]:
    """Summarize a Flask-style request without logging headers or values."""
    summary: dict[str, Any] = {
        'method': getattr(req, 'method', None),
        'path': getattr(req, 'path', None),
        'content_type': getattr(req, 'content_type', None),
        'content_length': getattr(req, 'content_length', None),
    }

    query_keys = payload_keys(getattr(req, 'args', None))
    form_keys = payload_keys(getattr(req, 'form', None))
    json_keys = payload_keys(payload)
    if query_keys:
        summary['query_keys'] = query_keys
    if form_keys:
        summary['form_keys'] = form_keys
    if json_keys:
        summary['payload_keys'] = json_keys
    elif payload is not None:
        summary['payload_type'] = type(payload).__name__
    return summary


def mask_phone(value: Any) -> str | None:
    """Keep only the last four digits of a phone-like value for correlation."""
    if value is None:
        return None
    digits = ''.join(char for char in str(value) if char.isdigit())
    return f"***{digits[-4:]}" if digits else '***'


# Matches the credentials in a ``scheme://user:pass@host`` URL. SignalWire
# echoes our own signed callback URLs back inside post-prompt payloads
# (swaig_log[].delayed_post_response), so any raw payload we persist or serve
# can carry the install's WEBHOOK_AUTH credential verbatim — which is also the
# INTERNAL_AUTH fallback and the ctk signing key. Base64/JSON is not redaction.
# Userinfo can contain neither whitespace, '/', '@', nor a quote — a quote would
# mean the match ran past a JSON string boundary. The user half also excludes
# ':' so it cannot swallow the separator.
_CRED_USER_CHARS = r"[^\s/@:\"']"
_CRED_PASS_CHARS = r"[^\s/@\"']"
_CREDENTIAL_URL_RE = re.compile(
    '(://)' + _CRED_USER_CHARS + '+:' + _CRED_PASS_CHARS + '+(@)'
)


def scrub_embedded_credentials(payload: Any) -> Any:
    """Replace ``://user:pass@`` with ``://***:***@`` anywhere in a payload.

    Operates on the JSON serialization so it reaches arbitrarily nested
    values without walking the structure. Returns the input unchanged when
    it is not JSON-serializable or contains no credential-shaped URL, so
    callers can use it unconditionally. The debug value of these payloads is
    the URL *shape*; the secret has none.
    """
    if payload is None:
        return None
    try:
        serialized = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return payload
    scrubbed = _CREDENTIAL_URL_RE.sub(r'\1***:***\2', serialized)
    if scrubbed == serialized:
        return payload
    try:
        return json.loads(scrubbed)
    except ValueError:
        return payload
