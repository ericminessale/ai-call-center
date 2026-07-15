"""
Hosted-demo endpoints — public surface for the demo landing flow.

Phase 1 tenancy: these routes keep their paths and response shapes (the
frontend's authStore/demoApi contract is unchanged) but are backed by
per-visitor WORKSPACES instead of the old shared persona pool:

  - ``GET  /api/config/runtime`` — public (no auth). Tells the
    frontend whether this instance is hosted-demo mode and exposes the
    demo phone numbers for the landing card. Always available; returns
    ``demo_mode: false`` on a normal clone-and-own deployment.
  - ``POST /api/demo/start``     — gated 404 in production. In tenancy
    mode: resumes the cookie's workspace or provisions a fresh one
    (queues/KB/config cloned from the template workspace), mints a JWT
    for the workspace's admin user, sets the session cookie, returns
    ``{access_token, refresh_token, user}``.
  - ``POST /api/demo/heartbeat`` — refreshes workspace liveness + the
    WebRTC seat lease while a visitor's tab is open.
  - ``POST /api/demo/end``       — releases the workspace (expires it,
    bumps the JWT epoch, frees the seat), clears the cookie.

Session semantics: see :mod:`app.services.workspace_provision` and
:mod:`app.services.workspace_session`. The visitor's JWTs carry
``{persona: true, wsid: <workspace public_id>, epoch}`` — both token
verification paths reject them the moment the workspace is released or
expires.

When neither TENANCY_MODE nor DEMO_MODE is set, every demo route returns
``404`` — we don't even hint that a demo path exists on a
production-shape instance.
"""

from __future__ import annotations

import logging
import secrets

from flask import Blueprint, jsonify, request, make_response

from app.services.workspace_provision import (
    provision_workspace,
    release_workspace,
    resume_workspace,
)
from app.services.workspace_session import get_workspace_epoch
from app.utils.demo_config import is_demo_mode, runtime_config
from app.utils.decorators import require_auth
from app.utils.jwt_utils import generate_tokens
from app.utils.rate_limit import rate_limit

logger = logging.getLogger(__name__)

demo_bp = Blueprint('demo', __name__)


# Cookie name carrying the visitor's anonymous session token. Random
# URL-safe secret. HttpOnly so JS can't read it; SameSite=Lax so
# cross-site embeds can't provision on someone's behalf. The cookie is
# the workspace-resume credential, so its lifetime tracks the workspace
# idle TTL (7 days) rather than the old 24h/5-min lease asymmetry —
# only its sha256 ever touches the database.
_SESSION_COOKIE = 'demo_session'
_SESSION_COOKIE_MAX_AGE = 7 * 24 * 60 * 60


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


def _mint_visitor_tokens(workspace, user) -> dict:
    """JWTs for a workspace visitor. verify_token + the flask-jwt-extended
    blocklist loader cross-reference persona/wsid/epoch against the live
    workspace session on every auth-gated request, so the tokens only work
    while the workspace is alive AND its epoch hasn't been bumped by a
    release."""
    return generate_tokens(
        user.id,
        extra_claims={
            'persona': True,
            'wsid': workspace.public_id,
            'epoch': get_workspace_epoch(workspace.public_id),
        },
    )


# ---------------------------------------------------------------------------
# Public — runtime config
# ---------------------------------------------------------------------------


@demo_bp.route('/config/runtime', methods=['GET'])
def get_runtime_config():
    """Public runtime config the frontend consults on app boot.

    Unauthenticated by design — the frontend must be able to render the
    landing card before a session exists. Never include secrets here.

    Tenancy (Phase 2): branding resolves from the visitor's session
    cookie when it binds to a live workspace, so a visitor who set their
    own product name/colors sees them again on reload/return — while a
    cookie-less first visit gets the platform branding (no per-tenant
    hostnames in v1, §8.3). Read-only resolution: loading the landing
    page must not extend the workspace's life.
    """
    ws_id = None
    if is_demo_mode():
        try:
            from app.services.workspace_provision import peek_workspace_id
            ws_id = peek_workspace_id(_request_session_token())
        except Exception:
            ws_id = None
    if ws_id is not None:
        from app.tenancy import workspace_context
        with workspace_context(ws_id):
            return jsonify(runtime_config())
    return jsonify(runtime_config())


# ---------------------------------------------------------------------------
# Demo session lifecycle
# ---------------------------------------------------------------------------


@demo_bp.route('/demo/start', methods=['POST'])
@rate_limit('demo_start', limit=10, window_seconds=60)
def start_demo_session():
    """Resume or provision the visitor's workspace.

    Side effects:
      - sets the demo session cookie if not present
      - resumes the cookie's live workspace, or creates + template-seeds a
        fresh one (own queues, own editable KB, own config — no colleagues)
      - mints a JWT for the workspace's admin ``User`` row
      - returns the same shape ``authApi.login`` does, so the frontend
        can hand the response straight to the existing auth handlers
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()

    session_token = _request_session_token() or _new_session_token()
    result = provision_workspace(session_token)

    if result is None:
        # Global workspace cap reached — the tenancy analog of the old
        # pool-exhausted 503. Honest answer + short retry hint.
        logger.warning("demo/start: MAX_WORKSPACES cap reached")
        resp = make_response(
            jsonify({'error': 'Demo currently full — please try again in a few minutes.'}),
            503,
        )
        # Don't set the cookie yet — they have no workspace, no need to track.
        return resp

    workspace, user = result
    tokens = _mint_visitor_tokens(workspace, user)
    body = {
        'message': 'Demo session started',
        'user': user.to_dict(),
        'workspace': workspace.to_dict(),
        **tokens,
    }
    response = make_response(jsonify(body), 200)
    _set_session_cookie(response, session_token)
    logger.info(
        "demo/start: workspace %s for session %s",
        workspace.public_id, session_token[:8],
    )
    return response


@demo_bp.route('/demo/heartbeat', methods=['POST'])
def heartbeat_demo_session():
    """Keep the visitor's workspace + WebRTC seat alive while the tab is open.

    Idempotent. Returns ``{ok: true}`` on a refresh, ``404`` when the
    workspace has expired/been released (frontend should re-call
    /api/demo/start for a fresh one).
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    if not session_token:
        return jsonify({'error': 'No active demo session'}), 404
    result = resume_workspace(session_token)
    if result is None:
        return jsonify({'error': 'Session expired'}), 404
    workspace, user = result
    # Seat lease + phone-verify bindings ride the same heartbeat the old
    # persona lease used. seat_held=False means no live seat lease (never
    # leased, or the TTL lapsed — and the seat password rotates on
    # re-claim, so a lapsed browser's registration is dead); the FE should
    # re-hit /api/fabric/token before its next WebRTC use.
    seat_held = False
    try:
        from app.services.seat_lease import heartbeat_seat_for_user
        seat_held = bool(heartbeat_seat_for_user(user.id))
    except Exception:
        pass
    try:
        from app.services.demo_verify import refresh_bindings
        refresh_bindings(user.workspace_id)
    except Exception:
        pass
    # Workspace lifetime rides the heartbeat (Phase 5 expiry UX): the touch
    # above may have just extended expires_at, so the banner's countdown
    # stays honest without another request.
    return jsonify({'ok': True, 'seat_held': seat_held, 'workspace': workspace.to_dict()})


@demo_bp.route('/demo/end', methods=['POST'])
def end_demo_session():
    """Explicitly release the visitor's workspace.

    Called on explicit "leave demo" actions. Always returns 200; the
    workspace may already be expired, that's fine — the cookie still gets
    cleared so a fresh "Start demo" click provisions a new workspace.
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    released = False
    if session_token:
        released = release_workspace(session_token)
    response = make_response(jsonify({'ok': True, 'released': released}), 200)
    _clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Phone-number verification (pairing-code flow)
# ---------------------------------------------------------------------------


def _require_demo_visitor():
    """Return (visitor_user, None) or (None, error_response).

    Verify endpoints are only meaningful for a workspace-scoped hosted-demo
    visitor. Gate on tenancy mode + a workspace-bound user so a platform
    user can't mint pairing codes. Must be used after @require_auth
    (reads request.current_user).
    """
    if not is_demo_mode():
        return None, _refuse_when_demo_off()
    user = getattr(request, 'current_user', None)
    if user is None or user.workspace_id is None:
        return None, (jsonify({'error': 'Not a demo session'}), 403)
    return user, None


@demo_bp.route('/demo/verify/pairing-code', methods=['POST'])
@rate_limit('demo_verify_code', limit=10, window_seconds=60)
@require_auth
def create_pairing_code():
    """Issue (or refresh) the visitor's 6-digit pairing code.

    The visitor TEXTS this code to the demo number; the inbound-SMS webhook
    (``webhooks.sms_inbound``) matches it and binds the sender's number to
    their WORKSPACE (§6.2 — the code carries the requesting user too, so
    inbound attribution can stamp Call.user_id). Inbound-only — no outbound
    SMS, so no messaging campaign or A2P/fraud surface. One live code per
    workspace; issuing a new one invalidates the old.
    """
    visitor, err = _require_demo_visitor()
    if err:
        return err
    from app.services.demo_verify import generate_pairing_code
    code = generate_pairing_code(visitor.workspace_id, visitor.id)
    if not code:
        return jsonify({'error': 'Could not generate a code — try again.'}), 503
    return jsonify({'code': code}), 200


@demo_bp.route('/demo/verify/status', methods=['GET'])
@require_auth
def get_verify_status():
    """Current verification state for the visitor: live code + masked number."""
    visitor, err = _require_demo_visitor()
    if err:
        return err
    from app.services.demo_verify import verify_status
    return jsonify(verify_status(visitor.workspace_id)), 200


@demo_bp.route('/demo/status', methods=['GET'])
def get_demo_session_status():
    """Lightweight status probe — does this session still have a live workspace?

    Useful for the frontend to detect "expired while idle" on page focus
    and prompt for a fresh start. Response keys keep the pre-tenancy names
    the frontend consumes (``leased``/``persona``).
    """
    if not is_demo_mode():
        return _refuse_when_demo_off()
    session_token = _request_session_token()
    if not session_token:
        return jsonify({'leased': False, 'persona': None, 'workspace': None})
    result = resume_workspace(session_token)
    return jsonify({
        'leased': result is not None,
        'persona': result[1].to_dict() if result else None,
        'workspace': result[0].to_dict() if result else None,
    })
