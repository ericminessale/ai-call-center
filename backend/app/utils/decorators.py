from functools import wraps
from flask import request, jsonify
from app.models import User
from app.utils.jwt_utils import verify_token


def require_auth(f):
    """Decorator to require authentication for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get token from Authorization header
        auth_header = request.headers.get('Authorization')

        if not auth_header:
            return jsonify({'error': 'No authorization header'}), 401

        # Extract token from "Bearer <token>"
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

        # Add user to request context
        request.current_user = user

        return f(*args, **kwargs)

    return decorated_function


def require_role(*allowed_roles):
    """Decorator to require specific user roles. Must be used AFTER @require_auth.

    Usage:
        @require_auth
        @require_role('supervisor', 'admin')
        def protected_endpoint():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(request, 'current_user') or not request.current_user:
                return jsonify({'error': 'Authentication required'}), 401
            if request.current_user.role not in allowed_roles:
                return jsonify({
                    'error': 'Insufficient permissions',
                    'required_roles': list(allowed_roles),
                    'current_role': request.current_user.role
                }), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_permission(*required_flags):
    """Decorator requiring one or more capability flags. Must be used AFTER @require_auth.

    All listed flags must evaluate truthy for the current user (role defaults
    merged with per-user overrides). See User.effective_permissions().

    Usage:
        @require_auth
        @require_permission('can_listen_ai_calls')
        def monitor_ai_call():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(request, 'current_user') or not request.current_user:
                return jsonify({'error': 'Authentication required'}), 401
            missing = [
                flag for flag in required_flags
                if not request.current_user.has_permission(flag)
            ]
            if missing:
                return jsonify({
                    'error': 'Missing required permissions',
                    'required_permissions': list(required_flags),
                    'missing_permissions': missing,
                }), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def validate_json(*expected_args):
    """Decorator to validate JSON request body."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not request.is_json:
                return jsonify({'error': 'Content-Type must be application/json'}), 400

            data = request.get_json()
            if not data:
                return jsonify({'error': 'No JSON data provided'}), 400

            missing = [arg for arg in expected_args if arg not in data]
            if missing:
                return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

            return f(*args, **kwargs)
        return wrapper
    return decorator