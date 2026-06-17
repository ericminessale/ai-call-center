"""
DEMO_MODE configuration helpers — single source of truth for whether
the running instance is the public-facing hosted demo.

The default for any clone-and-own deployment is ``DEMO_MODE`` unset (or
"false"). In that case every helper in this module short-circuits to
the equivalent of "this is a normal production-shape instance" and no
demo-specific behavior activates anywhere.

When ``DEMO_MODE=true`` is set in the environment, the hosted-demo
behaviors gate on through these helpers. See HOSTED DEMO MODE in
``memory/roadmap.md`` for the broader plan.

DO NOT add demo-specific logic anywhere that doesn't go through one of
these helpers. The whole point of the gate is that turning the env var
off restores the canonical clone-and-own behavior with no other code
changes.
"""

from __future__ import annotations

import os


# Reserved role string for demo-pool agents. Users with this role are
# never returned from the standard User Management list and never
# receive admin grants. Real human admins/supervisors/agents have
# ``admin`` / ``supervisor`` / ``agent`` as before.
DEMO_AGENT_ROLE = 'demo_agent'


def is_demo_mode() -> bool:
    """True if the running instance is the public hosted demo.

    Driven by the ``DEMO_MODE`` env var (case-insensitive). Anything
    other than ``"true"`` (including unset) is treated as production.
    Read at every call so an ops-side env change takes effect on the
    next request without a code redeploy — desirable for operators who
    want to flip the demo off in an emergency.
    """
    return os.getenv('DEMO_MODE', '').strip().lower() == 'true'


def demo_phone_numbers() -> list[dict]:
    """The demo phone numbers + labels surfaced on the landing card.

    Driven by ``DEMO_PHONE_NUMBERS`` env var (comma-separated
    ``label|+E164`` pairs). Example::

        DEMO_PHONE_NUMBERS=AI Line|+15551234567,Sales Direct|+15555678900

    Returns ``[]`` when not configured. Visitor will still see a
    landing card explaining the demo, just without specific numbers.
    """
    raw = os.getenv('DEMO_PHONE_NUMBERS', '').strip()
    if not raw:
        return []
    out: list[dict] = []
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair or '|' not in pair:
            continue
        label, number = pair.split('|', 1)
        label, number = label.strip(), number.strip()
        if label and number:
            out.append({'label': label, 'number': number})
    return out


def demo_pool_size() -> int:
    """How many demo personas to seed for the lease pool.

    M1 only seeds the rows; the lease layer ships in M2. Defaults to
    20; the boss can override via ``DEMO_POOL_SIZE`` if telemetry shows
    we need more.
    """
    raw = os.getenv('DEMO_POOL_SIZE', '20').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 20
    # Floor at 1, ceiling at 100 to prevent surprises.
    return max(1, min(n, 100))


def runtime_config() -> dict:
    """Public-safe runtime config for the frontend.

    Exposed via ``GET /api/config/runtime`` (no auth) so the UI can
    decide whether to render the login form or the demo landing card,
    which phone numbers to show, etc. Never include secrets here —
    this endpoint is intentionally unauthenticated.
    """
    branding = None
    try:
        from app.models.system_config import SystemConfig
        branding = SystemConfig.get_branding_config()
    except Exception:
        branding = None

    return {
        'demo_mode': is_demo_mode(),
        'demo_phone_numbers': demo_phone_numbers() if is_demo_mode() else [],
        # White-label branding (IMP-02) — public by design: the login page
        # must render the brand before any auth exists. Name/logo/colors
        # only, never secrets.
        'branding': branding,
    }


# Standard 403 body emitted when an action is refused in demo mode
# (outbound dial, destructive deletes, etc.). Frontend keys off
# ``code: demo_blocked`` to render a generic "not available in demo"
# toast — verb-specific phrasing isn't needed because the user
# already knows what action they tried to take.
DEMO_BLOCKED_RESPONSE = {
    'error': 'That action is not available in demo mode.',
    'code': 'demo_blocked',
}


def block_in_demo_mode(f):
    """Decorator that refuses the request with 403 when DEMO_MODE=true.

    Use on any endpoint that should soft-fail in the public demo:
    outbound dial (so visitors can't ring real phone numbers),
    destructive deletes against seed fixtures (so visitors can't
    nuke the demo for everyone else), etc.

    The form/UI is still allowed to render — per the demo's
    transparency goal we show what the feature does, then refuse
    only the actual server-side mutation. The frontend axios
    interceptor catches the 403 and toasts.

    Production-shape clone-and-own deployments pass through
    unchanged when ``is_demo_mode()`` is false.

    NOTE on the WebRTC gap: this only catches server-mediated
    actions. Browser-direct Call Fabric SDK dial bypasses the
    backend entirely — frontend UI gating
    (CallFabricContext.makeCall) handles that path.
    """
    from functools import wraps

    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_demo_mode():
            from flask import jsonify
            return jsonify(DEMO_BLOCKED_RESPONSE), 403
        return f(*args, **kwargs)
    return wrapped
