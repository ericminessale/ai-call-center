"""Feature flags for capabilities that ship in the codebase but stay OFF
until they're production-ready.

Off by default EVERYWHERE (hosted demo AND clone-and-own) so neither a public
visitor nor a developer who pulls the repo lands on an unfinished path. An
operator opts in explicitly via the env var. Flags are read per-request so a
flip needs no redeploy.
"""

from __future__ import annotations

import os
from functools import wraps

from flask import jsonify


def coach_enabled() -> bool:
    """AI Coach (sidecar). OFF unless ``COACH_ENABLED=true``.

    The sidecar targets a pre-release SignalWire ``calling.ai_sidecar`` verb
    whose suggestion stream isn't verified end-to-end yet, and it bills per
    minute, so it stays behind this flag until it's production-ready. The
    frontend renders a "coming soon" affordance while it's off.
    """
    return os.getenv('COACH_ENABLED', '').strip().lower() == 'true'


COACH_DISABLED_RESPONSE = {
    'error': 'The AI Coach is not enabled on this deployment yet.',
    'code': 'coach_disabled',
}


def require_coach_enabled(f):
    """Refuse the request with 403 unless :func:`coach_enabled`.

    Stack this with ``@block_in_demo_mode`` on the coach endpoints: the coach
    is reachable only on a non-demo deployment that has explicitly set
    ``COACH_ENABLED=true``.
    """

    @wraps(f)
    def wrapped(*args, **kwargs):
        if not coach_enabled():
            return jsonify(COACH_DISABLED_RESPONSE), 403
        return f(*args, **kwargs)

    return wrapped
