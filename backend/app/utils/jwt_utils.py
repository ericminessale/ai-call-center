import jwt
from datetime import datetime, timedelta
from flask import current_app


def generate_tokens(user_id, extra_claims=None):
    """Generate access and refresh tokens for a user.

    Optional ``extra_claims`` (dict) get merged into BOTH the access and
    refresh payloads. SEC-03 uses this for demo personas, baking in
    ``persona=true`` + the current ``epoch`` so ``verify_token`` can reject
    tokens issued under a prior lease (see :mod:`app.services.demo_lease`).
    """
    access_payload = {
        'user_id': user_id,
        'sub': user_id,  # Flask-JWT-Extended requires 'sub' claim
        'exp': datetime.utcnow() + current_app.config['JWT_ACCESS_TOKEN_EXPIRES'],
        'type': 'access'
    }

    refresh_payload = {
        'user_id': user_id,
        'sub': user_id,  # Flask-JWT-Extended requires 'sub' claim
        'exp': datetime.utcnow() + current_app.config['JWT_REFRESH_TOKEN_EXPIRES'],
        'type': 'refresh'
    }

    if extra_claims:
        # Don't let extra claims clobber the structural fields (sub/exp/type)
        # — only fill in additional metadata.
        reserved = {'user_id', 'sub', 'exp', 'type'}
        safe = {k: v for k, v in extra_claims.items() if k not in reserved}
        access_payload.update(safe)
        refresh_payload.update(safe)

    access_token = jwt.encode(
        access_payload,
        current_app.config['JWT_SECRET_KEY'],
        algorithm='HS256'
    )

    refresh_token = jwt.encode(
        refresh_payload,
        current_app.config['JWT_SECRET_KEY'],
        algorithm='HS256'
    )

    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_in': current_app.config['JWT_ACCESS_TOKEN_EXPIRES'].total_seconds()
    }


def decode_token(token):
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(
            token,
            current_app.config['JWT_SECRET_KEY'],
            algorithms=['HS256']
        )
        return payload
    except jwt.ExpiredSignatureError:
        return {'error': 'Token has expired'}
    except jwt.InvalidTokenError:
        return {'error': 'Invalid token'}


def persona_claims_are_stale(payload):
    """SEC-03: True if a decoded persona token no longer matches the live
    lease state in Redis.

    Non-persona payloads (real users) are never stale — returns False.
    For payloads carrying ``persona=true``:
      1. The token's ``epoch`` claim must match the current persona epoch
         (bumped by ``demo_lease.release_lease``) — invalidates JWTs
         after an explicit release.
      2. There must be an active (non-TTL-expired) lease for the persona
         — invalidates JWTs after the lease lapses naturally.

    Shared by :func:`verify_token` (custom ``require_auth`` path) and the
    flask-jwt-extended ``token_in_blocklist_loader`` registered in
    ``create_app`` — so ``@jwt_required()`` routes (e.g. the Call Fabric
    token mint in ``api/fabric.py``) enforce the same lease invariant.
    """
    if not payload.get('persona'):
        return False
    # Lazy import — demo_lease pulls in Redis + models, which can
    # cause circular imports if loaded at module top.
    try:
        from app.services.demo_lease import get_persona_epoch, has_active_lease
    except Exception:
        # If demo_lease isn't importable for any reason, a persona
        # claim is suspicious — be paranoid, reject.
        return True
    uid = payload.get('user_id')
    if uid is None:
        return True
    # Epoch check — bumped on release_lease.
    if payload.get('epoch', -1) != get_persona_epoch(uid):
        return True
    # Active-lease check — covers TTL-expiry case.
    return not has_active_lease(uid)


def extra_claims_for_refresh(payload):
    """Claims to carry forward when re-minting tokens from a refresh token.

    generate_tokens() bakes identity-scope claims into BOTH tokens
    (persona/epoch today; workspace claims when tenancy lands). A bare
    ``generate_tokens(user.id)`` on /refresh silently drops them — the
    refreshed access token then skips persona_claims_are_stale entirely,
    outliving the lease it was scoped to.

    The epoch is COPIED from the already-validated payload, not re-read
    from Redis: within a legitimate session the epoch never changes, and
    re-reading opens a TOCTOU where the lease expires between validation
    and re-mint, another visitor claims the persona (bumping the epoch),
    and the re-read would stamp THEIR epoch onto the old visitor's new
    token. Copying keeps the stale value, which then correctly fails the
    epoch match. The payload must already have passed verify_token —
    this function does no staleness checking itself.
    """
    if not payload.get('persona'):
        return None
    return {
        'persona': True,
        'epoch': payload.get('epoch'),
    }


def verify_token(token, token_type='access'):
    """Verify a token and return the user_id if valid.

    SEC-03 extension: tokens minted for a demo persona (carrying
    ``persona=true``) are additionally checked against the live persona
    state in Redis via :func:`persona_claims_are_stale`.

    Non-persona tokens (real users) are unaffected and follow the
    original codepath.
    """
    payload = decode_token(token)

    if 'error' in payload:
        return None

    if payload.get('type') != token_type:
        return None

    if persona_claims_are_stale(payload):
        return None

    return payload.get('user_id')
