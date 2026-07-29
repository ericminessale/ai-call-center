"""The ``sub`` claim must be a string, and legacy int-sub tokens must survive.

PyJWT >= 2.10 enforces RFC 7519's StringOrURI for ``sub`` and raises
``InvalidSubjectError`` on decode. ``generate_tokens`` minted ``sub`` as an int,
so bumping PyJWT for its advisories made every token in the system undecodable —
a total auth outage, not a subtle regression.

The fix has two halves, both pinned here:
  1. mint ``sub`` as ``str(user_id)`` (spec-correct, and what Flask-JWT-Extended
     4.7 wants), with identity still read from the int ``user_id`` claim;
  2. skip the subject *type* check on decode, so tokens minted before the change
     keep working through their natural expiry instead of force-logging everyone
     out. That check has no security value here — no expected ``subject=`` is
     passed, and ``sub`` is never trusted for identity.
"""
from datetime import datetime, timedelta

import jwt
import pytest
from flask import Flask, jsonify
from flask_jwt_extended import JWTManager, get_jwt_identity, jwt_required

from app.utils import jwt_utils

# >= 32 bytes: PyJWT warns on shorter HMAC keys (RFC 7518 §3.2).
SECRET = 'unit-test-jwt-secret-key-0123456789-abcdefghijkl'


@pytest.fixture()
def jwt_app():
    app = Flask(__name__)
    app.config.update(
        JWT_SECRET_KEY=SECRET,
        JWT_ACCESS_TOKEN_EXPIRES=timedelta(hours=1),
        JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=30),
        # Mirrors create_app.
        JWT_VERIFY_SUB=False,
        TESTING=True,
    )
    JWTManager(app)

    @app.get('/who')
    @jwt_required()
    def who():
        return jsonify(raw=get_jwt_identity(), coerced=jwt_utils.current_user_id())

    return app


def _legacy_token(user_id=42, **extra):
    """A token in the pre-fix shape: integer ``sub``."""
    payload = {
        'user_id': user_id,
        'sub': user_id,
        'type': 'access',
        'exp': datetime.utcnow() + timedelta(hours=1),
    }
    payload.update(extra)
    return jwt.encode(payload, SECRET, algorithm='HS256')


def test_sub_is_minted_as_a_string(jwt_app):
    with jwt_app.app_context():
        token = jwt_utils.generate_tokens(42)['access_token']
        payload = jwt.decode(token, SECRET, algorithms=['HS256'])

    assert payload['sub'] == '42'
    assert isinstance(payload['sub'], str)
    # Identity stays an int on the claim the app actually reads.
    assert payload['user_id'] == 42


def test_minted_token_decodes_under_pyjwt_subject_validation(jwt_app):
    """The bug in one line: strict decode of our own token must not raise."""
    with jwt_app.app_context():
        token = jwt_utils.generate_tokens(42)['access_token']
        # options left at their defaults => verify_sub ON.
        assert jwt.decode(token, SECRET, algorithms=['HS256'])['sub'] == '42'
        assert jwt_utils.verify_token(token) == 42


def test_legacy_int_sub_token_still_verifies(jwt_app):
    with jwt_app.app_context():
        assert jwt_utils.verify_token(_legacy_token()) == 42


def test_jwt_required_accepts_both_token_shapes(jwt_app):
    with jwt_app.app_context():
        fresh = jwt_utils.generate_tokens(42)['access_token']
    client = jwt_app.test_client()

    for label, token in (('fresh', fresh), ('legacy', _legacy_token())):
        resp = client.get('/who', headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 200, f'{label}: {resp.get_json()}'
        # Whatever the wire shape, routes get a usable int for PK lookups.
        assert resp.get_json()['coerced'] == 42, label


def test_signature_and_expiry_are_still_enforced(jwt_app):
    """verify_sub=False must not have loosened anything that matters."""
    with jwt_app.app_context():
        token = jwt_utils.generate_tokens(42)['access_token']
        assert jwt_utils.verify_token(token[:-4] + 'AAAA') is None

        forged = jwt.encode(
            {'user_id': 42, 'sub': '42', 'type': 'access',
             'exp': datetime.utcnow() + timedelta(hours=1)},
            'not-the-real-secret-but-long-enough-to-not-warn', algorithm='HS256',
        )
        assert jwt_utils.verify_token(forged) is None

        expired = jwt.encode(
            {'user_id': 42, 'sub': '42', 'type': 'access',
             'exp': datetime.utcnow() - timedelta(seconds=1)},
            SECRET, algorithm='HS256',
        )
        assert jwt_utils.verify_token(expired) is None

        # Wrong token type (refresh presented as access).
        refresh = jwt_utils.generate_tokens(42)['refresh_token']
        assert jwt_utils.verify_token(refresh) is None
        assert jwt_utils.verify_token(refresh, token_type='refresh') == 42


def test_extra_claims_survive_and_sub_cannot_be_overridden(jwt_app):
    with jwt_app.app_context():
        token = jwt_utils.generate_tokens(
            42, extra_claims={'persona': True, 'wsid': 'ws-abc', 'epoch': 7,
                              'sub': 'attacker', 'user_id': 999},
        )['access_token']
        payload = jwt.decode(token, SECRET, algorithms=['HS256'])

    assert payload['sub'] == '42'
    assert payload['user_id'] == 42
    assert (payload['wsid'], payload['epoch']) == ('ws-abc', 7)


def test_current_user_id_is_none_for_a_non_numeric_subject(jwt_app):
    token = jwt.encode(
        {'user_id': 42, 'sub': 'not-a-number', 'type': 'access',
         'exp': datetime.utcnow() + timedelta(hours=1)},
        SECRET, algorithm='HS256',
    )
    resp = jwt_app.test_client().get(
        '/who', headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {'raw': 'not-a-number', 'coerced': None}


def test_current_user_id_raises_outside_a_verified_jwt(jwt_app):
    """Deliberate: a silent None would make a route missing @jwt_required look
    like a plain 'user not found' instead of a bug."""
    with jwt_app.test_request_context('/'):
        with pytest.raises(RuntimeError):
            jwt_utils.current_user_id()
