import os
from flask import request, jsonify
from app import db
from app.api import auth_bp
from app.models import User
from app.utils.jwt_utils import generate_tokens, verify_token
from app.utils.decorators import validate_json
from app.utils.demo_config import block_in_demo_mode
from app.utils.rate_limit import rate_limit
import re


def _public_registration_enabled() -> bool:
    """SEC-06 gate. Self-service /register is OFF by default — users get
    created by admin via /api/admin/users or seeded by demo persona setup.
    Operators who actively want open registration (e.g. a public-facing
    signup flow with their own captcha/ratelimit layer) opt in by setting
    ``ALLOW_PUBLIC_REGISTRATION=true`` in .env.

    Default-off is the right shape because: (a) every existing real
    deployment uses admin-managed users, (b) the hosted-demo path
    bypasses /register entirely via /demo/start, (c) leaving it open
    exposes a public account-creation surface with no rate limit,
    captcha, or email verification — trivially abusable for resource
    creation."""
    return os.getenv('ALLOW_PUBLIC_REGISTRATION', 'false').strip().lower() == 'true'


@auth_bp.route('/register', methods=['POST'])
@block_in_demo_mode
@rate_limit('register', limit=5, window_seconds=60)
@validate_json('email', 'password')
def register():
    """Register a new user.

    SEC-06 fix (2026-06-02 audit): now gated behind
    ALLOW_PUBLIC_REGISTRATION (default off). When disabled, returns 403
    pointing the caller at /api/admin/users (the admin-managed creation
    path). Stronger password policy applies regardless of the gate when
    the endpoint IS enabled.
    """
    if not _public_registration_enabled():
        return jsonify({
            'error': 'Public registration is disabled',
            'detail': (
                'User accounts are created by an admin via /api/admin/users. '
                'Set ALLOW_PUBLIC_REGISTRATION=true in .env to enable this '
                'endpoint (recommended only behind your own captcha + rate-limit '
                'layer).'
            ),
        }), 403

    data = request.get_json()
    email = data.get('email').lower().strip()
    password = data.get('password')

    # Validate email format
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return jsonify({'error': 'Invalid email format'}), 400

    # SEC-06: strengthen password policy from 8-char-only to a length +
    # composition check. Length alone (8 chars) is insufficient for a
    # public registration endpoint. Demand 12+ chars AND a mix of two
    # character classes — common-sense baseline without the false
    # precision of cracker-resistance theatre.
    if len(password) < 12:
        return jsonify({'error': 'Password must be at least 12 characters long'}), 400
    if not (re.search(r'[A-Za-z]', password) and re.search(r'\d|[^A-Za-z0-9]', password)):
        return jsonify({
            'error': 'Password must contain at least one letter and one digit or symbol',
        }), 400

    # Check if user exists
    if User.find_by_email(email):
        return jsonify({'error': 'Email already registered'}), 409

    # Create new user
    user = User(email=email)
    user.set_password(password)

    try:
        db.session.add(user)
        db.session.commit()

        # Generate tokens
        tokens = generate_tokens(user.id)

        return jsonify({
            'message': 'User registered successfully',
            'user': user.to_dict(),
            **tokens
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to register user'}), 500


@auth_bp.route('/login', methods=['POST'])
@rate_limit('login', limit=10, window_seconds=60)
@validate_json('email', 'password')
def login():
    """Login a user.

    SEC-06: rate-limited per client IP — the hosted demo leaves this
    endpoint reachable (real admins sign into the demo instance too),
    so it needs brute-force protection on the public internet.
    """
    data = request.get_json()
    email = data.get('email').lower().strip()
    password = data.get('password')

    # Find user
    user = User.find_by_email(email)
    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid email or password'}), 401

    # Check if user is active
    if not user.is_active:
        return jsonify({'error': 'Account is deactivated'}), 403

    # Generate tokens
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Login successful',
        'user': user.to_dict(),
        **tokens
    }), 200


@auth_bp.route('/refresh', methods=['POST'])
@validate_json('refresh_token')
def refresh():
    """Refresh access token using refresh token."""
    data = request.get_json()
    refresh_token = data.get('refresh_token')

    # Verify refresh token
    user_id = verify_token(refresh_token, token_type='refresh')
    if not user_id:
        return jsonify({'error': 'Invalid or expired refresh token'}), 401

    # Get user
    user = User.find_by_id(user_id)
    if not user or not user.is_active:
        return jsonify({'error': 'User not found or inactive'}), 401

    # Generate new tokens
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Token refreshed successfully',
        **tokens
    }), 200


@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    """Get current user information."""
    from app.utils.decorators import require_auth

    # Get token from Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No authorization header'}), 401

    try:
        token = auth_header.split(' ')[1]
    except IndexError:
        return jsonify({'error': 'Invalid authorization header format'}), 401

    # Verify token
    user_id = verify_token(token)
    if not user_id:
        return jsonify({'error': 'Invalid or expired token'}), 401

    # Get user
    user = User.find_by_id(user_id)
    if not user or not user.is_active:
        return jsonify({'error': 'User not found or inactive'}), 401

    return jsonify({
        'user': user.to_dict()
    }), 200


@auth_bp.route('/me/languages', methods=['PUT'])
def update_my_languages():
    """Self-serve: set the languages I speak (BCP-47 codes).

    Used by the routing layer to prefer language-matched agents.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No authorization header'}), 401
    try:
        token = auth_header.split(' ')[1]
    except IndexError:
        return jsonify({'error': 'Invalid authorization header format'}), 401

    user_id = verify_token(token)
    if not user_id:
        return jsonify({'error': 'Invalid or expired token'}), 401

    user = User.find_by_id(user_id)
    if not user or not user.is_active:
        return jsonify({'error': 'User not found or inactive'}), 401

    data = request.get_json() or {}
    languages = data.get('languages')

    if not isinstance(languages, list) or not languages:
        return jsonify({'error': 'languages must be a non-empty list of BCP-47 strings'}), 400
    if not all(isinstance(l, str) and l for l in languages):
        return jsonify({'error': 'languages must contain non-empty strings'}), 400

    try:
        user.languages = languages
        db.session.commit()
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500