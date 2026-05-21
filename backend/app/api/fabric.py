"""
Call Fabric API endpoints for browser-based calling.
Handles subscriber token generation and subscriber management via the
canonical signalwire-sdk REST client.
"""

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import logging
import os

from app.services import signalwire_client as sw_client

logger = logging.getLogger(__name__)

fabric_bp = Blueprint('fabric', __name__)

FABRIC_APPLICATION_ID = os.getenv('FABRIC_APPLICATION_ID')


_DUPLICATE_EMAIL_CODES = {'value_not_unique', 'not_unique_within_project'}


def _is_duplicate_email_error(err: sw_client.SignalWireRestError) -> bool:
    """Detect SignalWire's 'email already in use' error shape."""
    body = getattr(err, 'body', None) or {}
    if not isinstance(body, dict):
        return False
    for e in body.get('errors', []) or []:
        if e.get('code') in _DUPLICATE_EMAIL_CODES and e.get('attribute') == 'email':
            return True
    return False


def _find_subscriber_by_email(email):
    """Return the subscriber resource dict for a given email, or None.

    SignalWire's subscriber list returns a Fabric resource wrapper with the
    real subscriber fields nested under `subscriber`; the top-level `email`
    is always null. We match against the nested field.
    """
    try:
        resp = sw_client.get_client().fabric.subscribers.list(page_size=100)
    except sw_client.SignalWireRestError as e:
        logger.error(f"Failed to list subscribers: {e}")
        return None
    items = resp.get('data', []) if isinstance(resp, dict) else resp
    for item in items:
        nested = item.get('subscriber') or {}
        if (item.get('email') or nested.get('email')) == email:
            # Flatten useful nested fields up so callers see a consistent shape.
            flat = {**item}
            for k in ('email', 'username', 'first_name', 'last_name'):
                if nested.get(k) and not flat.get(k):
                    flat[k] = nested.get(k)
            return flat
    return None


def _update_subscriber_password(subscriber_id, new_password):
    """Rotate an existing subscriber's password. True on success."""
    try:
        sw_client.get_client().fabric.subscribers.update(
            subscriber_id, password=new_password,
        )
        return True
    except sw_client.SignalWireRestError as e:
        logger.error(f"Failed to update subscriber password: {e}")
        return False


def _create_permanent_subscriber(user):
    """Create (or link to existing) a Fabric subscriber for the given user.

    Returns dict with id/username/password/address. Raises on failure.
    """
    import secrets
    from datetime import datetime

    password = secrets.token_urlsafe(32)

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
            "department": "general",
        },
    }

    client = sw_client.get_client()
    try:
        subscriber_data = client.fabric.subscribers.create(**payload)
    except sw_client.SignalWireRestError as e:
        if _is_duplicate_email_error(e):
            logger.info(f"Subscriber with email {user.email} already exists, linking...")
            existing = _find_subscriber_by_email(user.email)
            if not existing:
                raise Exception("Subscriber exists but could not be found")
            if not _update_subscriber_password(existing.get('id'), password):
                raise Exception("Failed to update existing subscriber password")
            subscriber_data = existing
            logger.info(f"Linked to existing subscriber: {existing.get('id')}")
        else:
            logger.error(f"Failed to create subscriber: {e}")
            raise Exception(f"Failed to create subscriber: {e}")

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

    # Fetch the subscriber's REAL Fabric address from SignalWire.
    # Old code fabricated this as "/private/agent-{user.id}" — that string
    # was NEVER registered with the platform, so any SWML using it as a
    # connect target failed silently (Bug J). SignalWire actually
    # auto-materializes one address per subscriber, slugged from the
    # subscriber's display_name (e.g. "System Administrator" →
    # "/private/system-administrator"). The format is opaque to us; the
    # only safe thing is to ask the platform what it created.
    subscriber_id = subscriber_data.get('id')
    fabric_address = f"/private/agent-{user.id}"  # Fallback if list fails.
    try:
        addrs_resp = sw_client.get_client().fabric.subscribers.list_addresses(subscriber_id)
        addr_list = addrs_resp.get('data', []) if isinstance(addrs_resp, dict) else addrs_resp
        if addr_list:
            first = addr_list[0]
            # `name` is the slug; build the /private/<name> form. If the
            # platform ever returns multi-channel addresses, the audio
            # channel string under `channels.audio` is the most precise.
            real_name = first.get('name')
            if real_name:
                fabric_address = f"/private/{real_name}"
            logger.info(
                f"Subscriber {subscriber_id}: real Fabric address resolved "
                f"to {fabric_address}"
            )
        else:
            logger.warning(
                f"Subscriber {subscriber_id} has no addresses yet — falling "
                f"back to fabricated agent-{user.id}. Re-run subscriber sync "
                f"after the platform materializes one."
            )
    except Exception as e:
        logger.error(
            f"Failed to list addresses for subscriber {subscriber_id}; "
            f"using fabricated /private/agent-{user.id}: {e}"
        )

    user.signalwire_subscriber_id = subscriber_id
    user.signalwire_username = username  # Keep original for token generation
    user.set_subscriber_password(password)  # Encrypted storage
    user.signalwire_address = fabric_address
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
        try:
            token_data = sw_client.get_client().fabric.tokens.create_subscriber_token(
                reference=reference,
                password=password,
            )
        except sw_client.SignalWireRestError as e:
            logger.error(f"Failed to get subscriber token: {e}")
            return jsonify({'error': 'Failed to generate token'}), 500

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
        try:
            resp = sw_client.get_client().fabric.subscribers.list(page_size=100)
        except sw_client.SignalWireRestError as e:
            logger.error(f"Failed to list subscribers: {e}")
            return jsonify({'error': 'Failed to list subscribers'}), 500

        subscribers = resp.get('data', []) if isinstance(resp, dict) else resp

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