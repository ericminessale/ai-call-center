"""
Hosted-demo endpoints — public surface for the demo landing flow.

Routes:
  - ``GET  /api/config/runtime`` — public (no auth). Tells the
    frontend whether this instance is in DEMO_MODE and exposes the
    demo phone numbers to display on the landing card. Always
    available; returns ``demo_mode: false`` on a normal clone-and-own
    deployment.
  - ``POST /api/demo/start``     — gated 404 in production. In demo
    mode: leases a demo persona for the visitor's anonymous session,
    mints a JWT for that persona, sets the session cookie, returns
    ``{access_token, refresh_token, user}``.
  - ``POST /api/demo/heartbeat`` — refreshes the lease TTL while a
    visitor's tab is open. Frontend calls this on an interval.
  - ``POST /api/demo/end``       — releases the lease, clears cookie.
    Called on browser unload / explicit "leave demo" actions.

Lease semantics: see :mod:`app.services.demo_lease`. The defensive
invariant is that the backend never mints a Call Fabric token for a
persona while another session holds its lease, so two visitors can
never operate as the same agent identity simultaneously.

When DEMO_MODE is off, every demo route returns ``404`` — we don't
even hint that a demo path exists on a production-shape instance.
"""

from __future__ import annotations

import logging
import secrets

from flask import Blueprint, jsonify, request, make_response

from app.services.demo_lease import (
    get_lease_for_session,
    heartbeat_lease,
    lease_persona,
    release_lease,
)
from app.utils.demo_config import DEMO_AGENT_ROLE, is_demo_mode, runtime_config
from app.utils.decorators import require_auth
from app.utils.jwt_utils import generate_tokens
from app.utils.rate_limit import rate_limit

logger = logging.getLogger(__name__)

demo_bp = Blueprint('demo', __name__)


# Cookie name carrying the visitor's anonymous session token. Random
# UUID. HttpOnly so JS can't read it; SameSite=Lax so cross-site
# embeds can't lease on someone's behalf. Cookie outlives the lease
# (24h vs 5min) — a stale cookie just gets a fresh lease on next start.
_SESSION_COOKIE = 'demo_session'
_SESSION_COOKIE_MAX_AGE = 24 * 60 * 60  # 24h


def _request_session_token() -> str | None:
    """Return the anonymous session token from the request cookie, or None."""
    return request.cookies.get(_SESSION_COOKIE)


def _new_session_token() -> str:
    """Generate a fresh URL-safe session token for a new visitor."""
    return secrets.token_urlsafe(24)


def _set_session_cookie(response, token: str) -> None:
    """Attach the session cookie to a response.

    Secure flag tracks the request scheme — set on HTTPS, omitted on
    plain HTTP for local dev. Production always lands behind TLS.
    """
    response.set_cookie(
        _SESSION_COOKIE,
        value=token,
        max_age=_SESSION_COOKIE_MAX_AGE,
        secure=request.is_secure,
        httponly=True,
        samesite='Lax',
        path='/',
    )


def _clear_session_cookie(response) -> None:
    response.set_cookie(
        _SESSION_COOKIE, value='', max_age=0,
        secure=request.is_secure, httponly=True, samesite='Lax', path='/',
    )


def _refuse_when_demo_off():
    """Standard 404 used by every demo-only route."""
    return jsonify({'error': 'Not found'}), 404


# ---------------------------------------------------------------------------
# Public — runtime config
# ---------------------------------------------------------------------------


@demo_bp.route('/config/runtime', methods=['GET'])
def get_runtime_config():
    """Public runtime config the frontend consults on app boot.

    Unauthenticated by design — the frontend must be able to render the
    landing card before a session exists. Never include secrets here.
    """
    return jsonify(runtime_config())


# ---------------------------------------------------------------------------
# Demo session lifecycle
# ---------------------------------------------------------------------------


@demo_bp.route('/demo/start', methods=['POST'])
@rate_limit('demo_start', limit=10, window_seconds=60)
def start_demo_session():
    """Lease a demo persona for the visitor's anonymous session.

    Side effects:
      - sets the demo session cookie if not present
      - claims a free persona from the pool (or refreshes the existing
        lease for repeat clicks / page reloads)
      - mints a JWT for the leased persona's ``User`` row
      - returns the same shape ``authApi.login`` does, so the frontend
        can hand the response straight to the existing auth handlers
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()

    session_token = _request_session_token() or _new_session_token()
    persona = lease_persona(session_token)

    if persona is None:
        # Pool exhausted — every persona is held by another session.
        # 503 + a short retry hint is the honest answer.
        logger.warning(
            "demo/start: pool exhausted (every persona is leased)"
        )
        resp = make_response(
            jsonify({'error': 'Demo currently full — please try again in a few minutes.'}),
            503,
        )
        # Don't set the cookie yet — they have no lease, no need to track.
        return resp

    # SEC-03: bake the persona marker + current epoch into the JWT.
    # verify_token cross-references both claims against demo_lease's Redis
    # state on every auth-gated request, so the token only works while
    # the lease is alive AND the epoch hasn't been bumped by a release.
    from app.services.demo_lease import get_persona_epoch
    tokens = generate_tokens(
        persona.id,
        extra_claims={
            'persona': True,
            'epoch': get_persona_epoch(persona.id),
        },
    )
    body = {
        'message': 'Demo session started',
        'user': persona.to_dict(),
        **tokens,
    }
    response = make_response(jsonify(body), 200)
    _set_session_cookie(response, session_token)
    logger.info(
        "demo/start: persona %s leased to session %s",
        persona.email, session_token[:8],
    )
    return response


@demo_bp.route('/demo/heartbeat', methods=['POST'])
def heartbeat_demo_session():
    """Refresh the lease TTL while the visitor's tab is open.

    Idempotent. Returns ``{ok: true}`` on a refresh, ``404`` when the
    lease has already expired (frontend should re-call /api/demo/start
    to lease a fresh persona).
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    if not session_token:
        return jsonify({'error': 'No active demo session'}), 404
    if not heartbeat_lease(session_token):
        return jsonify({'error': 'Lease expired'}), 404
    return jsonify({'ok': True})


@demo_bp.route('/demo/end', methods=['POST'])
def end_demo_session():
    """Explicitly release the visitor's lease.

    Called on ``beforeunload`` / explicit "leave demo" actions. Always
    returns 200; the lease may already be expired, that's fine — the
    cookie still gets cleared so a fresh "Start demo" click leases a
    new persona.
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    released = False
    if session_token:
        released = release_lease(session_token)
    response = make_response(jsonify({'ok': True, 'released': released}), 200)
    _clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Phone-number verification (pairing-code flow)
# ---------------------------------------------------------------------------


def _require_demo_persona():
    """Return (persona_user, None) or (None, error_response).

    Verify endpoints are only meaningful for a leased demo persona. Gate on
    DEMO_MODE + the demo_agent role so a real user can't mint pairing codes.
    Must be used after @require_auth (reads request.current_user).
    """
    if not is_demo_mode():
        return None, _refuse_when_demo_off()
    user = getattr(request, 'current_user', None)
    if user is None or (user.role or '') != DEMO_AGENT_ROLE:
        return None, (jsonify({'error': 'Not a demo session'}), 403)
    return user, None


@demo_bp.route('/demo/verify/pairing-code', methods=['POST'])
@rate_limit('demo_verify_code', limit=10, window_seconds=60)
@require_auth
def create_pairing_code():
    """Issue (or refresh) the visitor's 6-digit pairing code.

    The visitor TEXTS this code to the demo number; the inbound-SMS webhook
    (``webhooks.sms_inbound``) matches it and binds the sender's number to
    their persona. Inbound-only — no outbound SMS, so no messaging campaign
    or A2P/fraud surface. One live code per persona; issuing a new one
    invalidates the old.
    """
    persona, err = _require_demo_persona()
    if err:
        return err
    from app.services.demo_verify import generate_pairing_code
    code = generate_pairing_code(persona.id)
    if not code:
        return jsonify({'error': 'Could not generate a code — try again.'}), 503
    return jsonify({'code': code}), 200


@demo_bp.route('/demo/verify/status', methods=['GET'])
@require_auth
def get_verify_status():
    """Current verification state for the visitor: live code + masked number."""
    persona, err = _require_demo_persona()
    if err:
        return err
    from app.services.demo_verify import verify_status
    return jsonify(verify_status(persona.id)), 200


@demo_bp.route('/demo/status', methods=['GET'])
def get_demo_session_status():
    """Lightweight status probe — does this session still hold a lease?

    Useful for the frontend to detect "lease expired while idle" on
    page focus and prompt for a fresh start. Not strictly required.
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    if not session_token:
        return jsonify({'leased': False})
    persona = get_lease_for_session(session_token)
    return jsonify({
        'leased': persona is not None,
        'persona': persona.to_dict() if persona else None,
    })
