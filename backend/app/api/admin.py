from flask import request, jsonify
from app import db
from app.api import admin_bp
from app.models import Call, Transcription, User
from app.models.system_config import SystemConfig
from app.models.document import DocumentCollection, Document, AgentCollectionAssignment
from app.models.queue import Queue, QueueAgentAssignment
from app.utils.decorators import require_auth, require_role
from app.utils.jwt_utils import verify_token
from app.utils.url_utils import get_base_url

VALID_USER_ROLES = ('admin', 'supervisor', 'agent')


@admin_bp.before_request
def _enforce_admin_role():
    """Require admin role for every /api/admin/* route.

    Replaces per-route @require_auth; populates request.current_user so existing
    handlers can read it without changes.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No authorization header'}), 401
    try:
        token = auth_header.split(' ', 1)[1]
    except IndexError:
        return jsonify({'error': 'Invalid authorization header format'}), 401

    user_id = verify_token(token)
    if not user_id:
        return jsonify({'error': 'Invalid or expired token'}), 401

    user = User.find_by_id(user_id)
    if not user or not user.is_active:
        return jsonify({'error': 'User not found or inactive'}), 401

    if user.role != 'admin':
        return jsonify({
            'error': 'Admin role required',
            'current_role': user.role,
        }), 403

    request.current_user = user
import logging
import re
import requests as http_requests
import os
from base64 import b64encode

logger = logging.getLogger(__name__)

# AI agents URL for internal communication (port 8081 for admin/reindex API)
AI_AGENTS_ADMIN_URL = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')

# SignalWire API credentials for subscriber management
SIGNALWIRE_SPACE = os.getenv('SIGNALWIRE_SPACE')
SIGNALWIRE_PROJECT_KEY = os.getenv('SIGNALWIRE_PROJECT_ID')
SIGNALWIRE_TOKEN = os.getenv('SIGNALWIRE_API_TOKEN')


def _get_sw_auth_headers():
    """Get authentication headers for SignalWire API."""
    credentials = f"{SIGNALWIRE_PROJECT_KEY}:{SIGNALWIRE_TOKEN}"
    auth = b64encode(credentials.encode()).decode('ascii')
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {auth}'
    }


# =============================================================================
# Existing: Clear Calls
# =============================================================================

@admin_bp.route('/clear-calls', methods=['POST'])
@require_auth
def clear_calls():
    """Clear all stale calls from the database."""
    logger.info(f"CLEAR CALLS REQUEST from user: {request.current_user.id}")

    try:
        from datetime import datetime, timedelta
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        stale_calls = db.session.query(Call).filter(
            db.and_(
                Call.ended_at.is_(None),
                Call.created_at < one_hour_ago,
                Call.status.in_(['created', 'ringing', 'answered', 'initiated'])
            )
        ).all()

        logger.info(f"Found {len(stale_calls)} stale calls to clean up")

        deleted_transcriptions = 0
        for call in stale_calls:
            transcriptions = Transcription.query.filter_by(call_id=call.id).all()
            for t in transcriptions:
                db.session.delete(t)
                deleted_transcriptions += 1

        deleted_calls = len(stale_calls)
        for call in stale_calls:
            logger.info(f"Deleting stale call: {call.id}, status={call.status}, created_at={call.created_at}")
            db.session.delete(call)

        db.session.commit()

        logger.info(f"Successfully deleted {deleted_calls} calls and {deleted_transcriptions} transcriptions")

        return jsonify({
            'success': True,
            'deleted_calls': deleted_calls,
            'deleted_transcriptions': deleted_transcriptions,
            'message': f'Cleared {deleted_calls} stale calls from database'
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to clear calls: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to clear calls: {str(e)}'}), 500


# =============================================================================
# Agent Routing Config
# =============================================================================

@admin_bp.route('/agent-config', methods=['GET'])
@require_auth
def get_agent_config():
    """Get current agent routing configuration."""
    try:
        config = SystemConfig.get_routing_config()

        # Also fetch available agents from ai_control module
        from app.api.ai_control import AI_AGENTS
        return jsonify({
            'config': config,
            'available_agents': AI_AGENTS,
        }), 200
    except Exception as e:
        logger.error(f"Failed to get agent config: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/agent-config', methods=['PUT'])
@require_auth
def update_agent_config():
    """Update agent routing configuration."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        user_id = request.current_user.id
        updated = []

        for slot in ['initial_handler', 'sales_specialist', 'support_specialist']:
            if slot in data:
                key = f'route.{slot}'
                SystemConfig.set(key, data[slot], user_id=user_id)
                updated.append(slot)

        return jsonify({
            'success': True,
            'updated': updated,
            'config': SystemConfig.get_routing_config(),
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update agent config: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Queue Management
# =============================================================================

@admin_bp.route('/queues', methods=['GET'])
@require_auth
def list_queues():
    """List all queues with agent counts."""
    try:
        queues = Queue.query.order_by(Queue.display_name).all()
        return jsonify({
            'queues': [q.to_dict(include_agent_count=True) for q in queues]
        }), 200
    except Exception as e:
        logger.error(f"Failed to list queues: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/queues', methods=['POST'])
@require_auth
def create_queue():
    """Create a new queue."""
    try:
        data = request.get_json()
        if not data or not data.get('slug') or not data.get('display_name'):
            return jsonify({'error': 'slug and display_name are required'}), 400

        slug = data['slug'].lower().strip()
        if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', slug):
            return jsonify({'error': 'slug must be lowercase alphanumeric with hyphens'}), 400

        if Queue.find_by_slug(slug):
            return jsonify({'error': f'Queue "{slug}" already exists'}), 409

        queue = Queue(
            slug=slug,
            display_name=data['display_name'],
            description=data.get('description'),
            routing_strategy=data.get('routing_strategy', 'round_robin'),
            ai_agent_route=data.get('ai_agent_route'),
            default_priority=data.get('default_priority', 5),
            sla_threshold_seconds=data.get('sla_threshold_seconds', 60),
            max_wait_before_ai_fallback=data.get('max_wait_before_ai_fallback', 120),
        )
        db.session.add(queue)
        db.session.commit()

        from app import socketio
        socketio.emit('queue_config_changed', {'action': 'created', 'queue': queue.to_dict()})

        return jsonify({'queue': queue.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create queue: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/queues/<int:queue_id>', methods=['PUT'])
@require_auth
def update_queue(queue_id):
    """Update queue settings."""
    try:
        queue = Queue.query.get(queue_id)
        if not queue:
            return jsonify({'error': 'Queue not found'}), 404

        data = request.get_json()
        for field in ['display_name', 'description', 'is_active', 'routing_strategy',
                      'ai_agent_route', 'default_priority', 'sla_threshold_seconds',
                      'max_wait_before_ai_fallback']:
            if field in data:
                setattr(queue, field, data[field])

        db.session.commit()

        from app import socketio
        socketio.emit('queue_config_changed', {'action': 'updated', 'queue': queue.to_dict()})

        return jsonify({'queue': queue.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update queue: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/queues/<int:queue_id>', methods=['DELETE'])
@require_auth
def delete_queue(queue_id):
    """Delete a queue and its agent assignments."""
    try:
        queue = Queue.query.get(queue_id)
        if not queue:
            return jsonify({'error': 'Queue not found'}), 404

        slug = queue.slug
        db.session.delete(queue)
        db.session.commit()

        from app import socketio
        socketio.emit('queue_config_changed', {'action': 'deleted', 'queue_slug': slug})

        return jsonify({'success': True, 'message': f'Queue "{slug}" deleted'}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete queue: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Queue-Agent Assignments
# =============================================================================

@admin_bp.route('/queues/<int:queue_id>/agents', methods=['GET'])
@require_auth
def get_queue_agents(queue_id):
    """Get agents assigned to a queue."""
    try:
        queue = Queue.query.get(queue_id)
        if not queue:
            return jsonify({'error': 'Queue not found'}), 404

        assignments = QueueAgentAssignment.query.filter_by(queue_id=queue_id).all()
        return jsonify({
            'queue': queue.to_dict(),
            'assignments': [a.to_dict() for a in assignments],
        }), 200
    except Exception as e:
        logger.error(f"Failed to get queue agents: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/queues/<int:queue_id>/agents', methods=['PUT'])
@require_auth
def update_queue_agents(queue_id):
    """Replace all agent assignments for a queue.
    Body: { "assignments": [{ "user_id": 1, "skill_level": 7 }, ...] }
    """
    try:
        queue = Queue.query.get(queue_id)
        if not queue:
            return jsonify({'error': 'Queue not found'}), 404

        data = request.get_json()
        if not data or 'assignments' not in data:
            return jsonify({'error': 'assignments array required'}), 400

        QueueAgentAssignment.query.filter_by(queue_id=queue_id).delete()

        for item in data['assignments']:
            user_id = item.get('user_id')
            if not user_id:
                continue
            user = User.query.get(user_id)
            if not user:
                continue
            assignment = QueueAgentAssignment(
                queue_id=queue_id,
                user_id=user_id,
                skill_level=item.get('skill_level', 5),
                is_activated=False,
            )
            db.session.add(assignment)

        db.session.commit()

        assignments = QueueAgentAssignment.query.filter_by(queue_id=queue_id).all()
        return jsonify({
            'success': True,
            'assignments': [a.to_dict() for a in assignments],
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update queue agents: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Document Collections
# =============================================================================

@admin_bp.route('/collections', methods=['GET'])
@require_auth
def list_collections():
    """List all document collections with document counts."""
    try:
        collections = DocumentCollection.query.order_by(DocumentCollection.id).all()
        return jsonify({
            'collections': [c.to_dict() for c in collections]
        }), 200
    except Exception as e:
        logger.error(f"Failed to list collections: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/collections', methods=['POST'])
@require_auth
def create_collection():
    """Create a new document collection."""
    try:
        data = request.get_json()
        if not data or not data.get('name') or not data.get('display_name'):
            return jsonify({'error': 'name and display_name are required'}), 400

        # Check for duplicate name
        existing = DocumentCollection.query.filter_by(name=data['name']).first()
        if existing:
            return jsonify({'error': f'Collection with name "{data["name"]}" already exists'}), 409

        collection = DocumentCollection(
            name=data['name'],
            display_name=data['display_name'],
            description=data.get('description', ''),
        )
        db.session.add(collection)
        db.session.commit()

        return jsonify({'collection': collection.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create collection: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/collections/<int:collection_id>', methods=['PUT'])
@require_auth
def update_collection(collection_id):
    """Update a document collection's metadata."""
    try:
        collection = DocumentCollection.query.get(collection_id)
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404

        data = request.get_json()
        if data.get('display_name'):
            collection.display_name = data['display_name']
        if 'description' in data:
            collection.description = data['description']

        db.session.commit()
        return jsonify({'collection': collection.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update collection: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/collections/<int:collection_id>', methods=['DELETE'])
@require_auth
def delete_collection(collection_id):
    """Delete a collection and all its documents."""
    try:
        collection = DocumentCollection.query.get(collection_id)
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404

        # Also remove any agent assignments for this collection
        AgentCollectionAssignment.query.filter_by(collection_id=collection_id).delete()

        db.session.delete(collection)
        db.session.commit()

        return jsonify({'success': True, 'message': f'Collection "{collection.name}" deleted'}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete collection: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Documents
# =============================================================================

@admin_bp.route('/collections/<int:collection_id>/documents', methods=['GET'])
@require_auth
def list_documents(collection_id):
    """List all documents in a collection."""
    try:
        collection = DocumentCollection.query.get(collection_id)
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404

        documents = Document.query.filter_by(collection_id=collection_id).order_by(Document.updated_at.desc()).all()
        return jsonify({
            'documents': [d.to_dict() for d in documents],
            'collection': collection.to_dict(include_doc_count=False),
        }), 200
    except Exception as e:
        logger.error(f"Failed to list documents: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/collections/<int:collection_id>/documents', methods=['POST'])
@require_auth
def create_document(collection_id):
    """Create a new document in a collection."""
    try:
        collection = DocumentCollection.query.get(collection_id)
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404

        data = request.get_json()
        if not data or not data.get('title') or not data.get('content'):
            return jsonify({'error': 'title and content are required'}), 400

        doc = Document(
            collection_id=collection_id,
            title=data['title'],
            content=data['content'],
            is_published=False,
        )
        db.session.add(doc)
        db.session.commit()

        return jsonify({'document': doc.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create document: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/documents/<int:doc_id>', methods=['PUT'])
@require_auth
def update_document(doc_id):
    """Update a document's title and/or content."""
    try:
        doc = Document.query.get(doc_id)
        if not doc:
            return jsonify({'error': 'Document not found'}), 404

        data = request.get_json()
        if data.get('title'):
            doc.title = data['title']
        if 'content' in data:
            doc.content = data['content']
        if 'is_published' in data:
            doc.is_published = data['is_published']

        from datetime import datetime
        doc.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'document': doc.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update document: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/documents/<int:doc_id>', methods=['DELETE'])
@require_auth
def delete_document(doc_id):
    """Delete a document."""
    try:
        doc = Document.query.get(doc_id)
        if not doc:
            return jsonify({'error': 'Document not found'}), 404

        db.session.delete(doc)
        db.session.commit()

        return jsonify({'success': True}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete document: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Reindex (proxy to AI agents)
# =============================================================================

@admin_bp.route('/collections/<int:collection_id>/reindex', methods=['POST'])
@require_auth
def reindex_collection(collection_id):
    """Trigger reindexing of a collection's documents into pgvector."""
    try:
        collection = DocumentCollection.query.get(collection_id)
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404

        # Get all documents in the collection
        documents = Document.query.filter_by(collection_id=collection_id).all()

        if not documents:
            return jsonify({'error': 'No documents to index'}), 400

        # Prepare payload for AI agents reindex endpoint
        payload = {
            'collection_name': collection.name,
            'documents': [
                {'title': d.title, 'content': d.content}
                for d in documents
            ],
        }

        # Call AI agents reindex endpoint
        logger.info(f"Triggering reindex for collection '{collection.name}' with {len(documents)} documents")
        resp = http_requests.post(
            f"{AI_AGENTS_ADMIN_URL}/reindex",
            json=payload,
            timeout=120,  # Embedding can take time
        )

        if resp.status_code == 200:
            result = resp.json()
            # Mark all documents as published
            for doc in documents:
                doc.is_published = True
            db.session.commit()

            return jsonify({
                'success': True,
                'collection': collection.name,
                'documents_indexed': len(documents),
                'chunks_indexed': result.get('chunks_indexed', 0),
            }), 200
        else:
            error_msg = resp.text
            logger.error(f"Reindex failed: {resp.status_code} - {error_msg}")
            return jsonify({
                'error': f'Reindex failed: {error_msg}',
                'status_code': resp.status_code,
            }), 502

    except http_requests.exceptions.ConnectionError:
        logger.error("Cannot reach AI agents service for reindex")
        return jsonify({'error': 'Cannot reach AI agents service. Is it running?'}), 503
    except http_requests.exceptions.Timeout:
        logger.error("Reindex request timed out")
        return jsonify({'error': 'Reindex timed out. Try again or reduce document count.'}), 504
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to reindex: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Agent-Collection Assignments
# =============================================================================

@admin_bp.route('/agent-assignments', methods=['GET'])
@require_auth
def get_agent_assignments():
    """Get all agent-to-collection assignments."""
    try:
        # Optional filter by agent_id
        agent_id = request.args.get('agent_id')

        query = AgentCollectionAssignment.query
        if agent_id:
            query = query.filter_by(agent_id=agent_id)

        assignments = query.all()

        return jsonify({
            'assignments': [a.to_dict() for a in assignments]
        }), 200
    except Exception as e:
        logger.error(f"Failed to get agent assignments: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/agent-assignments', methods=['PUT'])
@require_auth
def update_agent_assignments():
    """Update agent-to-collection assignments.

    Body: { "assignments": [{ "agent_id": "sales-ai", "collection_id": 1 }] }
    Replaces ALL assignments with the provided list.
    """
    try:
        data = request.get_json()
        if not data or 'assignments' not in data:
            return jsonify({'error': 'assignments array is required'}), 400

        # Clear existing assignments
        AgentCollectionAssignment.query.delete()

        # Insert new assignments
        for item in data['assignments']:
            if not item.get('agent_id') or not item.get('collection_id'):
                continue

            # Verify collection exists
            collection = DocumentCollection.query.get(item['collection_id'])
            if not collection:
                continue

            assignment = AgentCollectionAssignment(
                agent_id=item['agent_id'],
                collection_id=item['collection_id'],
            )
            db.session.add(assignment)

        db.session.commit()

        # Return updated assignments
        assignments = AgentCollectionAssignment.query.all()
        return jsonify({
            'success': True,
            'assignments': [a.to_dict() for a in assignments],
            'message': 'Agent restart required for changes to take effect',
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update agent assignments: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# User Management
# =============================================================================

@admin_bp.route('/users', methods=['GET'])
@require_auth
def list_users():
    """List all users for admin management."""
    try:
        users = User.query.order_by(User.created_at.desc()).all()
        return jsonify({
            'users': [u.to_dict() for u in users]
        }), 200
    except Exception as e:
        logger.error(f"Failed to list users: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>', methods=['PUT'])
@require_auth
def update_user(user_id):
    """Update a user's role. Admin-only (enforced at blueprint level)."""
    data = request.get_json() or {}
    new_role = data.get('role')

    if new_role not in VALID_USER_ROLES:
        return jsonify({
            'error': f'role must be one of: {", ".join(VALID_USER_ROLES)}'
        }), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Prevent admins from demoting themselves — avoids lockout when there is
    # only one admin left.
    if request.current_user.id == user_id and new_role != 'admin':
        return jsonify({
            'error': 'You cannot change your own role away from admin'
        }), 400

    try:
        user.role = new_role
        db.session.commit()
        logger.info(
            f"User {user_id} ({user.email}) role set to '{new_role}' "
            f"by admin {request.current_user.id}"
        )
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update user role: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@require_auth
def delete_user(user_id):
    """Delete a user, their SignalWire subscriber, and clean up FK references."""
    try:
        # Prevent self-deletion
        if request.current_user.id == user_id:
            return jsonify({'error': 'Cannot delete your own account'}), 400

        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        user_email = user.email

        # Step 1: Delete SignalWire Call Fabric subscriber if one exists
        sw_delete_error = None
        if user.signalwire_subscriber_id and SIGNALWIRE_SPACE:
            try:
                url = f"https://{SIGNALWIRE_SPACE}/api/fabric/subscribers/{user.signalwire_subscriber_id}"
                resp = http_requests.delete(url, headers=_get_sw_auth_headers(), timeout=10)
                if resp.status_code in [200, 204, 404]:
                    logger.info(f"SignalWire subscriber {user.signalwire_subscriber_id} deleted (status {resp.status_code})")
                else:
                    sw_delete_error = f"SignalWire subscriber delete returned {resp.status_code}"
                    logger.warning(sw_delete_error)
            except Exception as e:
                sw_delete_error = f"Failed to reach SignalWire: {str(e)}"
                logger.warning(sw_delete_error)
            # Continue with local deletion regardless

        # Step 2: Nullify FK references that would break on delete
        from app.models.call_leg import CallLeg
        from app.models.conference import Conference

        Call.query.filter_by(assigned_agent_id=user_id).update({'assigned_agent_id': None})
        CallLeg.query.filter_by(user_id=user_id).update({'user_id': None})
        Conference.query.filter_by(owner_user_id=user_id).update({'owner_user_id': None})
        SystemConfig.query.filter_by(updated_by=user_id).update({'updated_by': None})

        # Step 3: Delete the user (cascade handles calls via user_id FK)
        db.session.delete(user)
        db.session.commit()

        result = {
            'success': True,
            'message': f'User "{user_email}" deleted'
        }
        if sw_delete_error:
            result['sw_warning'] = sw_delete_error

        logger.info(f"User {user_id} ({user_email}) deleted by admin {request.current_user.id}")
        return jsonify(result), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete user {user_id}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to delete user: {str(e)}'}), 500


# =============================================================================
# Phone Number Management (uses SignalWire Fabric Resources API)
# =============================================================================

def _sw_rest_url():
    """Return the SignalWire REST API base URL (for phone numbers, etc.)."""
    return f"https://{SIGNALWIRE_SPACE}/api/relay/rest"


def _sw_fabric_url():
    """Return the SignalWire Fabric API base URL (for resources, webhooks, etc.)."""
    return f"https://{SIGNALWIRE_SPACE}/api/fabric"


def _find_or_create_swml_webhook(webhook_url):
    """Find an existing SWML webhook resource for our URL, or create one.

    Uses the Fabric Resources API at /api/fabric/resources/swml_webhooks.
    Returns the webhook resource ID, or None on failure.
    """
    fabric_url = _sw_fabric_url()
    headers = _get_sw_auth_headers()

    # Step 1: List existing SWML webhooks to see if one already points to our URL
    list_url = f"{fabric_url}/resources/swml_webhooks"
    logger.warning(f"[PHONE-DEBUG] Listing SWML webhooks at: {list_url}")
    list_resp = http_requests.get(list_url, headers=headers, timeout=15)

    logger.warning(f"[PHONE-DEBUG] List SWML webhooks response: {list_resp.status_code}")
    if list_resp.status_code == 200:
        resp_json = list_resp.json()
        webhooks = resp_json.get('data', resp_json.get('webhooks', []))
        logger.warning(f"[PHONE-DEBUG] Found {len(webhooks)} existing SWML webhooks, raw keys: {list(resp_json.keys())}")
        for wh in webhooks:
            logger.warning(f"[PHONE-DEBUG]   Webhook raw: {wh}")
            wh_url = wh.get('primary_request_url') or wh.get('request_url') or wh.get('url') or ''
            if wh_url.rstrip('/') == webhook_url.rstrip('/'):
                logger.warning(f"[PHONE-DEBUG] Found existing SWML webhook resource: {wh.get('id')}")
                return wh.get('id')
    else:
        logger.warning(f"[PHONE-DEBUG] List SWML webhooks failed: {list_resp.status_code} - {list_resp.text[:500]}")

    # Step 2: Create a new SWML webhook resource
    create_url = f"{fabric_url}/resources/swml_webhooks"
    create_payload = {
        'name': 'Call Center Inbound Handler',
        'primary_request_url': webhook_url,
    }
    logger.warning(f"[PHONE-DEBUG] Creating SWML webhook at: {create_url}")
    create_resp = http_requests.post(
        create_url,
        json=create_payload,
        headers=headers,
        timeout=15,
    )

    logger.warning(f"[PHONE-DEBUG] Create SWML webhook response: {create_resp.status_code} - {create_resp.text[:1000]}")
    if create_resp.status_code in (200, 201):
        result = create_resp.json()
        webhook_id = result.get('id')
        logger.warning(f"[PHONE-DEBUG] Created SWML webhook resource: {webhook_id} - full response: {result}")
        return webhook_id
    else:
        logger.error(f"Failed to create SWML webhook: {create_resp.status_code} - {create_resp.text[:1000]}")
        return None


def _assign_phone_number_direct(number_sid, webhook_url, headers):
    """Fallback: Try to assign phone number directly via PUT with call_handler.

    Tries 'swml_webhooks' as the call_handler value (matching laml_webhooks pattern).
    Returns (success, message) tuple.
    """
    rest_url = _sw_rest_url()
    for handler_value in ['swml_webhooks', 'swml_script']:
        update_url = f"{rest_url}/phone_numbers/{number_sid}"
        payload = {
            'call_handler': handler_value,
            'call_request_url': webhook_url,
        }
        logger.warning(f"[PHONE-DEBUG] Direct assign attempt with call_handler='{handler_value}' at: {update_url}")
        resp = http_requests.put(update_url, json=payload, headers=headers, timeout=15)
        logger.warning(f"[PHONE-DEBUG] Direct assign response: {resp.status_code} - {resp.text[:500]}")

        if resp.status_code == 200:
            return True, f'Phone number assigned with call_handler={handler_value}'
        else:
            logger.warning(f"Direct assign with call_handler='{handler_value}' failed: {resp.status_code}")

    return False, 'All direct assignment methods failed'


@admin_bp.route('/phone-numbers', methods=['GET'])
@require_auth
def list_phone_numbers():
    """List all phone numbers from SignalWire REST API."""
    try:
        if not SIGNALWIRE_SPACE or not SIGNALWIRE_PROJECT_KEY:
            return jsonify({'error': 'SignalWire credentials not configured'}), 500

        # Build webhook URL from EXTERNAL_URL
        base_url = get_base_url()
        webhook_url = f"{base_url}/api/swml/initial-call" if base_url else None

        # Call SignalWire REST API
        url = f"{_sw_rest_url()}/phone_numbers"
        resp = http_requests.get(
            url,
            headers=_get_sw_auth_headers(),
            params={'page_size': 100},
            timeout=15,
        )

        if resp.status_code != 200:
            logger.error(f"SignalWire API error listing phone numbers: {resp.status_code} - {resp.text}")
            return jsonify({'error': f'SignalWire API error: {resp.status_code}'}), 502

        data = resp.json()
        raw_numbers = data.get('data', [])

        # Log first number's raw structure for debugging
        if raw_numbers:
            logger.info(f"Phone number raw fields: {list(raw_numbers[0].keys())}")
            logger.debug(f"Phone number sample: {raw_numbers[0]}")

        phone_numbers = []
        for n in raw_numbers:
            # Try multiple possible field names for the webhook URL
            call_request_url = (
                n.get('call_request_url') or
                n.get('call_relay_context') or
                ''
            )
            call_handler = n.get('call_handler') or ''

            # Number is assigned if its webhook URL matches ours
            is_assigned = bool(
                webhook_url and call_request_url and
                webhook_url.rstrip('/') == call_request_url.rstrip('/')
            )
            phone_numbers.append({
                'sid': n.get('id'),
                'phone_number': n.get('number') or n.get('phone_number', ''),
                'friendly_name': n.get('name') or '',
                'voice_url': call_request_url,
                'call_handler': call_handler,
                'status_callback': n.get('call_status_callback_url') or '',
                'is_assigned': is_assigned,
            })

        return jsonify({
            'phone_numbers': phone_numbers,
            'webhook_url': webhook_url or '',
            'is_configured': bool(base_url),
        }), 200

    except http_requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot reach SignalWire API'}), 503
    except http_requests.exceptions.Timeout:
        return jsonify({'error': 'SignalWire API request timed out'}), 504
    except Exception as e:
        logger.error(f"Failed to list phone numbers: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/phone-numbers/<string:number_sid>', methods=['POST'])
@require_auth
def update_phone_number(number_sid):
    """Assign/unassign a phone number to the call center via SWML webhook resource."""
    try:
        data = request.get_json()
        action = data.get('action') if data else None

        if action not in ('assign', 'unassign'):
            return jsonify({'error': 'action must be "assign" or "unassign"'}), 400

        base_url = get_base_url()
        if action == 'assign' and not base_url:
            return jsonify({'error': 'EXTERNAL_URL not configured. Cannot assign webhook.'}), 400

        rest_url = _sw_rest_url()
        fabric_url = _sw_fabric_url()
        headers = _get_sw_auth_headers()

        if action == 'assign':
            webhook_url = f"{base_url}/api/swml/initial-call"

            # Strategy 1: Fabric Resources API — create SWML webhook + assign phone route
            webhook_id = _find_or_create_swml_webhook(webhook_url)
            if webhook_id:
                assign_url = f"{fabric_url}/resources/{webhook_id}/phone_routes"
                assign_payload = {'phone_number_id': number_sid}
                logger.warning(f"[PHONE-DEBUG] Assigning resource to phone route at: {assign_url} with payload: {assign_payload}")

                assign_resp = http_requests.post(
                    assign_url,
                    json=assign_payload,
                    headers=headers,
                    timeout=15,
                )

                logger.warning(f"[PHONE-DEBUG] Assign phone route response: {assign_resp.status_code} - {assign_resp.text[:500]}")

                if assign_resp.status_code in (200, 201):
                    logger.info(f"Assigned SWML webhook {webhook_id} to phone number {number_sid}")
                    return jsonify({
                        'success': True,
                        'phone_number': {
                            'sid': number_sid,
                            'is_assigned': True,
                        },
                        'message': 'Phone number assigned to call center (SWML handler via Fabric Resources)',
                    }), 200
                else:
                    logger.warning(f"Fabric Resources phone_routes failed, trying direct assignment fallback")

            # Strategy 2: Fallback — direct PUT on phone number with call_handler variants
            logger.info("Trying direct phone number assignment fallback...")
            success, message = _assign_phone_number_direct(number_sid, webhook_url, headers)
            if success:
                return jsonify({
                    'success': True,
                    'phone_number': {
                        'sid': number_sid,
                        'is_assigned': True,
                    },
                    'message': message,
                }), 200
            else:
                return jsonify({
                    'error': 'Failed to assign phone number. Fabric Resources API and direct assignment both failed.',
                    'details': message,
                }), 502

        else:
            # Unassign: Update phone number to clear handler
            update_resp = http_requests.put(
                f"{rest_url}/phone_numbers/{number_sid}",
                json={
                    'call_handler': 'laml_webhooks',
                    'call_request_url': '',
                },
                headers=headers,
                timeout=15,
            )

            if update_resp.status_code != 200:
                logger.error(f"Failed to unassign phone number: {update_resp.status_code} - {update_resp.text}")
                return jsonify({
                    'error': f'Failed to unassign: {update_resp.status_code}',
                    'details': update_resp.text,
                }), 502

            return jsonify({
                'success': True,
                'phone_number': {
                    'sid': number_sid,
                    'is_assigned': False,
                },
                'message': 'Phone number unassigned from call center',
            }), 200

    except http_requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot reach SignalWire API'}), 503
    except http_requests.exceptions.Timeout:
        return jsonify({'error': 'SignalWire API request timed out'}), 504
    except Exception as e:
        logger.error(f"Failed to update phone number: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/webhook-url', methods=['GET'])
@require_auth
def get_webhook_url():
    """Return the backend's webhook URL that phone numbers should point to."""
    base_url = get_base_url()
    return jsonify({
        'webhook_url': f"{base_url}/api/swml/initial-call" if base_url else '',
        'is_configured': bool(base_url),
    }), 200
