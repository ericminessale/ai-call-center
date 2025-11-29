from flask import request, jsonify
from app import db
from app.api import auth_bp
from app.models import User
from app.utils.jwt_utils import generate_tokens, verify_token
from app.utils.decorators import validate_json
import re


@auth_bp.route('/register', methods=['POST'])
@validate_json('email', 'password')
def register():
    """Register a new user."""
    data = request.get_json()
    email = data.get('email').lower().strip()
    password = data.get('password')

    # Validate email format
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return jsonify({'error': 'Invalid email format'}), 400

    # Validate password strength
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters long'}), 400

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
@validate_json('email', 'password')
def login():
    """Login a user."""
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