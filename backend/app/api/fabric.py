"""
Call Fabric API endpoints for browser-based calling
Handles subscriber token generation and call management
"""

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import requests
import os
from base64 import b64encode
import logging

logger = logging.getLogger(__name__)

fabric_bp = Blueprint('fabric', __name__)

# SignalWire configuration
SIGNALWIRE_SPACE = os.getenv('SIGNALWIRE_SPACE')
SIGNALWIRE_PROJECT_KEY = os.getenv('SIGNALWIRE_PROJECT_ID')
SIGNALWIRE_TOKEN = os.getenv('SIGNALWIRE_API_TOKEN')
FABRIC_APPLICATION_ID = os.getenv('FABRIC_APPLICATION_ID')  # Subscriber ID or application ID

def get_auth_headers():
    """Get authentication headers for SignalWire API."""
    credentials = f"{SIGNALWIRE_PROJECT_KEY}:{SIGNALWIRE_TOKEN}"
    auth = b64encode(credentials.encode()).decode('ascii')
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {auth}'
    }

def _find_subscriber_by_email(email):
    """
    Find an existing subscriber by email.
    Returns subscriber data dict or None if not found.
    """
    url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers"
    response = requests.get(url, headers=get_auth_headers())

    if response.status_code != 200:
        logger.error(f"Failed to list subscribers: {response.text}")
        return None

    subscribers = response.json().get('data', [])
    for sub in subscribers:
        if sub.get('email') == email:
            return sub
    return None


def _update_subscriber_password(subscriber_id, new_password):
    """
    Update an existing subscriber's password.
    Returns True on success, False on failure.
    """
    url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers/{subscriber_id}"
    payload = {"password": new_password}

    response = requests.put(url, json=payload, headers=get_auth_headers())

    if response.status_code not in [200, 204]:
        logger.error(f"Failed to update subscriber password: {response.text}")
        return False
    return True


def _create_permanent_subscriber(user):
    """
    Internal helper to create a permanent subscriber in SignalWire.
    If subscriber already exists (same email), links to existing one.
    Returns subscriber data dict or raises exception.
    """
    import secrets
    from datetime import datetime

    # Generate secure password for this subscriber
    password = secrets.token_urlsafe(32)

    # Create subscriber payload
    payload = {
        "email": user.email,
        "password": password,
        "first_name": user.name.split()[0] if user.name else 'Agent',
        "last_name": user.name.split()[-1] if user.name and len(user.name.split()) > 1 else '',
        "display_name": user.name or f"Agent {user.id}",
        "job_title": "Call Center Agent",
        "metadata": {
            "user_id": user.id,
            "role": user.role,
            "department": "general"
        }
    }

    # Call SignalWire API
    url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers"
    response = requests.post(
        url,
        json=payload,
        headers=get_auth_headers()
    )

    if response.status_code not in [200, 201]:
        # Check if it's a duplicate email error
        try:
            error_data = response.json()
            errors = error_data.get('errors', [])
            is_duplicate = any(
                e.get('code') == 'value_not_unique' and e.get('attribute') == 'email'
                for e in errors
            )
        except:
            is_duplicate = False

        if is_duplicate:
            logger.info(f"Subscriber with email {user.email} already exists, linking to existing...")

            # Find existing subscriber
            existing = _find_subscriber_by_email(user.email)
            if not existing:
                raise Exception("Subscriber exists but could not be found")

            logger.info(f"Found existing subscriber data: {existing}")

            # Update their password so we know what it is
            if not _update_subscriber_password(existing.get('id'), password):
                raise Exception("Failed to update existing subscriber password")

            subscriber_data = existing
            logger.info(f"Linked to existing subscriber: {existing.get('id')}")
        else:
            logger.error(f"Failed to create subscriber: {response.text}")
            raise Exception(f"Failed to create subscriber: {response.status_code}")
    else:
        subscriber_data = response.json()

    # Store subscriber info in user record
    # SignalWire API may use different field names for username/reference
    # For token generation, email can be used as the reference
    username = (
        subscriber_data.get('username') or
        subscriber_data.get('name') or
        subscriber_data.get('alias') or
        subscriber_data.get('reference') or
        subscriber_data.get('email')  # Email works as reference for tokens
    )

    if not username:
        logger.error(f"Could not determine username from subscriber data: {subscriber_data}")
        raise Exception("Subscriber data missing username/email field")

    # Build the fabric address
    # IMPORTANT: Call Fabric addresses must be alphanumeric with hyphens.
    # Email format like "/private/eric.minessale@gmail.com" is INVALID.
    # Use a sanitized format: "agent-{user_id}"
    fabric_address_name = f"agent-{user.id}"

    user.signalwire_subscriber_id = subscriber_data.get('id')
    user.signalwire_username = username  # Keep original for token generation
    user.set_subscriber_password(password)  # Encrypted storage
    user.signalwire_address = f"/private/{fabric_address_name}"
    user.fabric_subscriber_created_at = datetime.utcnow()

    logger.info(f"Subscriber fabric address: {user.signalwire_address} (token ref: {username})")

    from app import db
    db.session.commit()

    logger.info(f"Created/linked permanent subscriber for user {user.id}: {subscriber_data.get('id')}")

    return {
        'id': subscriber_data.get('id'),
        'username': subscriber_data.get('username'),
        'password': password,
        'address': user.signalwire_address
    }


@fabric_bp.route('/token', methods=['POST'])
@jwt_required()
def get_subscriber_token():
    """
    Generate a Call Fabric subscriber token for the authenticated user.
    Automatically creates a permanent subscriber on first use.
    """
    try:
        user_id = get_jwt_identity()
        from app.models import User
        user = User.query.filter_by(id=user_id).first()

        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Check if user has a permanent subscriber
        if not user.signalwire_subscriber_id:
            logger.info(f"User {user_id} has no subscriber, creating one...")
            try:
                subscriber = _create_permanent_subscriber(user)
            except Exception as e:
                logger.error(f"Failed to create subscriber: {e}")
                return jsonify({'error': 'Failed to create subscriber'}), 500
        else:
            logger.info(f"User {user_id} has existing subscriber: {user.signalwire_subscriber_id}")

        # Get permanent credentials
        reference = user.signalwire_username
        password = user.get_subscriber_password()

        if not reference or not password:
            logger.error(f"User {user_id} has subscriber ID but missing credentials")
            return jsonify({'error': 'Invalid subscriber credentials'}), 500

        # Request token from SignalWire using permanent credentials
        url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers/tokens"
        payload = {
            "reference": reference,
            "password": password
        }

        response = requests.post(
            url,
            json=payload,
            headers=get_auth_headers()
        )

        if response.status_code != 200:
            logger.error(f"Failed to get subscriber token: {response.text}")
            return jsonify({'error': 'Failed to generate token'}), 500

        token_data = response.json()

        logger.info(f"Generated token for permanent subscriber: {reference}")

        return jsonify({
            'token': token_data.get('token'),
            'expires_at': token_data.get('expires_at'),
            'reference': reference,
            'subscriber_id': user.signalwire_subscriber_id,
            'address': user.signalwire_address
        })

    except Exception as e:
        logger.error(f"Error generating subscriber token: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@fabric_bp.route('/subscriber/create', methods=['POST'])
@jwt_required()
def create_subscriber():
    """
    Manually create a permanent subscriber for the authenticated user.
    Note: Subscribers are now auto-created when getting tokens, so this is optional.
    """
    try:
        user_id = get_jwt_identity()
        from app.models import User
        user = User.query.filter_by(id=user_id).first()

        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Check if already has subscriber
        if user.signalwire_subscriber_id:
            return jsonify({
                'message': 'Subscriber already exists',
                'subscriber_id': user.signalwire_subscriber_id,
                'username': user.signalwire_username,
                'address': user.signalwire_address
            }), 200

        # Create permanent subscriber
        subscriber = _create_permanent_subscriber(user)

        return jsonify({
            'message': 'Subscriber created successfully',
            'subscriber_id': subscriber['id'],
            'username': subscriber['username'],
            'address': subscriber['address']
        }), 201

    except Exception as e:
        logger.error(f"Error creating subscriber: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@fabric_bp.route('/subscribers', methods=['GET'])
@jwt_required()
def list_subscribers():
    """
    List all subscribers (agents) in the space.
    Useful for showing available agents for transfers.
    """
    try:
        url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers"

        response = requests.get(
            url,
            headers=get_auth_headers()
        )

        if response.status_code != 200:
            logger.error(f"Failed to list subscribers: {response.text}")
            return jsonify({'error': 'Failed to list subscribers'}), 500

        subscribers = response.json().get('data', [])

        # Filter to only show agents (not system subscribers)
        agents = [
            {
                'id': sub.get('id'),
                'name': sub.get('display_name'),
                'email': sub.get('email'),
                'address': f"/private/{sub.get('username')}",
                'status': sub.get('status', 'offline'),  # Need to check actual status
                'metadata': sub.get('metadata', {})
            }
            for sub in subscribers
            if sub.get('metadata', {}).get('role') in ['agent', 'supervisor']
        ]

        return jsonify({'agents': agents})

    except Exception as e:
        logger.error(f"Error listing subscribers: {e}")
        return jsonify({'error': str(e)}), 500


@fabric_bp.route('/call/transfer', methods=['POST'])
@jwt_required()
def transfer_call():
    """
    Transfer an active call to another agent or queue.
    TODO: Implement using conference-based transfers.
    """
    return jsonify({'error': 'Transfer not yet implemented. Use conference-based routing instead.'}), 501


@fabric_bp.route('/call/record', methods=['POST'])
@jwt_required()
def toggle_recording():
    """
    Start or stop recording for an active call.
    TODO: Implement using SignalWire REST API.
    """
    return jsonify({'error': 'Recording control not yet implemented.'}), 501


@fabric_bp.route('/resources', methods=['GET'])
@jwt_required()
def list_resources():
    """
    List available Call Fabric resources (queues, AI agents, etc.)
    that agents can transfer calls to.
    """
    try:
        # This would query SignalWire for available resources
        # For now, return a structured list
        resources = {
            'queues': [
                {'name': 'Sales Queue', 'address': '/public/queue-sales', 'waiting': 0},
                {'name': 'Support Queue', 'address': '/public/queue-support', 'waiting': 0},
                {'name': 'Billing Queue', 'address': '/public/queue-billing', 'waiting': 0}
            ],
            'ai_agents': [
                {'name': 'Sales AI', 'address': '/public/ai-sales', 'available': True},
                {'name': 'Support AI', 'address': '/public/ai-support', 'available': True},
                {'name': 'FAQ Bot', 'address': '/public/ai-faq', 'available': True}
            ],
            'supervisors': [
                # Would query actual supervisor subscribers
            ]
        }

        return jsonify(resources)

    except Exception as e:
        logger.error(f"Error listing resources: {e}")
        return jsonify({'error': str(e)}), 500