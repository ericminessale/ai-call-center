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


def tenancy_mode_active() -> bool:
    """True when this instance runs the per-visitor-workspace hosted demo.

    Driven by ``TENANCY_MODE`` (the go-forward flag) or the legacy
    ``DEMO_MODE`` env var. The shared-floor persona model is gone
    (§10.4 of MULTI_TENANCY_DESIGN.md) so the two flags are synonyms
    now: hosted mode = workspaces. Read at every call so an ops-side
    env change takes effect on the next request without a redeploy.
    """
    return (
        os.getenv('TENANCY_MODE', '').strip().lower() == 'true'
        or os.getenv('DEMO_MODE', '').strip().lower() == 'true'
    )


def is_demo_mode() -> bool:
    """True if the running instance is the public hosted demo.

    Alias of :func:`tenancy_mode_active` since the Phase 1 tenancy
    refactor — every hosted-demo gate (verify flow, outbound caps,
    inbound limiter, recording default, registration block, runtime
    config) now keys off workspace mode. Kept as the name ~30 call
    sites use; new code should call tenancy_mode_active().
    """
    return tenancy_mode_active()


def call_is_persona_owned(call) -> bool:
    """True if this call is attributed to a specific demo persona.

    Phone-verification (see services/demo_verify) sets ``call.user_id`` to the
    leased persona for calls from a visitor's verified number. Such a call is
    PRIVATE to that visitor — the isolation checks use this to exclude it from
    the shared-floor allowances that otherwise let any demo persona watch any
    AI call. Shared/unattributed calls stay owned by the synthetic system user
    (role != demo_agent) and return False here. Cheap: reads the ORM backref,
    one PK lookup at most.
    """
    if call is None:
        return False
    owner = getattr(call, 'user', None)
    return owner is not None and (getattr(owner, 'role', '') or '') == DEMO_AGENT_ROLE


def demo_persona_self_scoped(user) -> bool:
    """True when ``user`` is a leased demo persona on the hosted demo.

    Demo personas carry the FULL permission set (see ROLE_PERMISSION_DEFAULTS
    in models/user.py) so the coach / recording / observer surfaces render and
    work — but their permissions are self-scoped: a flag never extends their
    reach to a call they don't own. Call sites that would honor a permission
    flag across calls must check this first and fall back to the ownership
    test (``demo_persona_call_guard`` for REST, inline owner checks for
    sockets). Always False outside demo mode, so clone-and-own deployments
    keep plain flag semantics.
    """
    if not is_demo_mode():
        return False
    return (getattr(user, 'role', '') or '') == DEMO_AGENT_ROLE


def demo_persona_owns_call(call, user) -> bool:
    """True when the demo persona owns this call.

    Ownership = assigned agent on the call, initiated it, or the call is
    attributed to them (``call.user_id``) — which is how phone-verification
    (services/demo_verify) marks inbound calls from a visitor's verified
    number. This is the "gated to the contact/number you verified" rule.
    """
    if call is None or user is None:
        return False
    return call.user_id == user.id or call.assigned_agent_id == user.id


def demo_persona_call_guard(call, user):
    """403 a demo persona acting on a call it doesn't own; None to allow.

    Companion to the permission flip in ROLE_PERMISSION_DEFAULTS['demo_agent']:
    apply AFTER @require_permission on any endpoint whose flag would otherwise
    let a persona reach another visitor's call (monitor, AI inject/hold,
    whisper escalation, ...). No-op for real users and outside demo mode.
    """
    if not demo_persona_self_scoped(user):
        return None
    if demo_persona_owns_call(call, user):
        return None
    from flask import jsonify
    return jsonify({
        'error': 'In the demo this action only works on your own calls.',
        'detail': (
            'Verify your phone number from the demo banner, then call in '
            'from it — calls from your verified number belong to you and '
            'unlock the full control surface.'
        ),
        'code': 'demo_scope',
    }), 403


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
