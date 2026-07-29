import jwt
from datetime import datetime, timedelta
from flask import current_app


def generate_tokens(user_id, extra_claims=None):
    """Generate access and refresh tokens for a user.

    Optional ``extra_claims`` (dict) get merged into BOTH the access and
    refresh payloads. Tenancy bakes in ``wsid`` (workspace public_id) for
    query scoping, and — for hosted-demo visitors — ``persona=true`` +
    the workspace ``epoch`` so ``verify_token`` can reject tokens issued
    under a prior session (see :mod:`app.services.workspace_session`).
    """
    access_payload = {
        'user_id': user_id,
        # Flask-JWT-Extended requires a 'sub' claim, and RFC 7519 makes it a
        # StringOrURI. PyJWT >= 2.10 enforces that on decode
        # (InvalidSubjectError: "Subject must be a string"), so the old int
        # here made every token undecodable the moment PyJWT was upgraded for
        # its CVEs. Identity is read from 'user_id' (an int) throughout —
        # 'sub' exists only to satisfy the two libraries.
        'sub': str(user_id),
        'exp': datetime.utcnow() + current_app.config['JWT_ACCESS_TOKEN_EXPIRES'],
        'type': 'access'
    }

    refresh_payload = {
        'user_id': user_id,
        # Flask-JWT-Extended requires a 'sub' claim, and RFC 7519 makes it a
        # StringOrURI. PyJWT >= 2.10 enforces that on decode
        # (InvalidSubjectError: "Subject must be a string"), so the old int
        # here made every token undecodable the moment PyJWT was upgraded for
        # its CVEs. Identity is read from 'user_id' (an int) throughout —
        # 'sub' exists only to satisfy the two libraries.
        'sub': str(user_id),
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
    """Decode and validate a JWT token.

    ``verify_sub`` is off deliberately. PyJWT's subject check only asserts
    that ``sub`` is a *string* (we pass no expected ``subject=``), so it
    carries no security value here — identity comes from the ``user_id``
    claim, never from ``sub``. Leaving it on would reject every token minted
    before the str(user_id) change above, i.e. force-log-out everyone holding
    a live access or 30-day refresh token at deploy time. Signature, exp and
    algorithm verification are all untouched.
    """
    try:
        payload = jwt.decode(
            token,
            current_app.config['JWT_SECRET_KEY'],
            algorithms=['HS256'],
            options={'verify_sub': False},
        )
        return payload
    except jwt.ExpiredSignatureError:
        return {'error': 'Token has expired'}
    except jwt.InvalidTokenError:
        return {'error': 'Invalid token'}


def current_user_id():
    """The authenticated user's id as an int, for ``@jwt_required()`` routes.

    ``get_jwt_identity()`` returns the raw ``sub`` claim, which is now a
    string (see :func:`generate_tokens`). Every consumer feeds it into an
    integer-PK lookup, so coerce once here rather than relying on the
    database's implicit cast.

    Returns None only for a present-but-unusable identity (non-numeric ``sub``).
    Called outside a verified-JWT request it propagates
    ``get_jwt_identity()``'s RuntimeError rather than degrading to None — a
    silent None here would turn "this route forgot @jwt_required" into a
    plausible-looking 404.
    """
    from flask_jwt_extended import get_jwt_identity

    raw = get_jwt_identity()
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def persona_claims_are_stale(payload):
    """SEC-03: True if a decoded visitor token no longer matches the live
    workspace state.

    Non-persona payloads (real users — clone-and-own logins, the platform
    operator admin) are never stale: their optional ``wsid`` claim scopes
    queries but carries no lifetime semantics, exactly like pre-tenancy
    real-user tokens. For payloads carrying ``persona=true`` (hosted-demo
    visitors), the workspace-session checks apply:
      1. The token's ``epoch`` claim must match the workspace's current
         epoch (bumped on release/reap — kills replayed JWTs).
      2. The workspace session must be alive (Redis fast path with a DB
         rehydrate — covers idle expiry).
    A persona token WITHOUT a ``wsid`` claim predates the workspace model
    (old persona-pool tokens) and is always stale — the pool is gone.

    Shared by :func:`verify_token` (custom ``require_auth`` path) and the
    flask-jwt-extended ``token_in_blocklist_loader`` registered in
    ``create_app`` — so ``@jwt_required()`` routes (e.g. the Call Fabric
    token mint in ``api/fabric.py``) enforce the same invariant.
    """
    if not payload.get('persona'):
        return False
    if payload.get('user_id') is None or not payload.get('wsid'):
        return True
    # Lazy import — workspace_session pulls in Redis + models, which can
    # cause circular imports if loaded at module top.
    try:
        from app.services.workspace_session import workspace_claims_are_stale
    except Exception:
        # If the session layer isn't importable for any reason, a visitor
        # claim is suspicious — be paranoid, reject.
        return True
    return workspace_claims_are_stale(payload)


def extra_claims_for_refresh(payload):
    """Claims to carry forward when re-minting tokens from a refresh token.

    generate_tokens() bakes identity-scope claims into BOTH tokens
    (persona/epoch today; workspace claims when tenancy lands). A bare
    ``generate_tokens(user.id)`` on /refresh silently drops them — the
    refreshed access token then skips persona_claims_are_stale entirely,
    outliving the lease it was scoped to.

    All claims are COPIED from the already-validated payload, not re-read
    from Redis/DB: within a legitimate session the epoch never changes, and
    re-reading opens a TOCTOU where the session expires between validation
    and re-mint, the workspace is re-provisioned (bumping the epoch), and
    the re-read would stamp the NEW epoch onto the old visitor's token.
    Copying keeps the stale value, which then correctly fails the epoch
    match. The payload must already have passed verify_token — this
    function does no staleness checking itself.

    ``wsid`` (workspace public_id) rides on BOTH visitor and real-user
    tokens (it's what scopes g.workspace_id), so it's copied outside the
    persona branch.
    """
    claims = {}
    if payload.get('wsid'):
        claims['wsid'] = payload['wsid']
    if payload.get('persona'):
        claims['persona'] = True
        claims['epoch'] = payload.get('epoch')
    return claims or None


def verify_token(token, token_type='access'):
    """Verify a token and return the user_id if valid.

    SEC-03 extension: tokens minted for a hosted-demo visitor (carrying
    ``persona=true``) are additionally checked against the live workspace
    session via :func:`persona_claims_are_stale`.

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
