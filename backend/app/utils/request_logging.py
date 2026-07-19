"""Privacy-safe request metadata for operational webhook logs.

Webhook bodies can contain phone numbers, transcripts, customer context, and
credentials embedded in callback URLs. Production logs should describe the
shape of a request without copying those values into a second data store.
"""

from __future__ import annotations

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
