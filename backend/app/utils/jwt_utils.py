import jwt
from datetime import datetime, timedelta
from flask import current_app


def generate_tokens(user_id):
    """Generate access and refresh tokens for a user."""
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


def verify_token(token, token_type='access'):
    """Verify a token and return the user_id if valid."""
    payload = decode_token(token)

    if 'error' in payload:
        return None

    if payload.get('type') != token_type:
        return None

    return payload.get('user_id')