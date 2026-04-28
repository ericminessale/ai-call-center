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
from app.services import signalwire_client as sw_client

from app.utils.demo_config import DEMO_AGENT_ROLE

# Roles a real admin is allowed to assign via the User Management UI.
# ``demo_agent`` is intentionally NOT in this set: the role is reserved
# for hosted-demo personas seeded at boot (see services/demo_seed.py),
# never something an admin should grant manually.
VALID_USER_ROLES = ('admin', 'supervisor', 'agent')


def _refuse_if_demo_persona(user) -> tuple[dict, int] | None:
    """Refuse mutation on a hosted-demo persona row.

    Demo agents are pool fixtures owned by the seed layer, not human
    team members. The admin UI never lists them and these endpoints
    won't touch them either, so a curl-with-the-id attempt also fails.
    Returns a (body, status) error tuple to bubble up, or None when the
    user is a real teammate that can be modified normally.
    """
    if user is not None and user.role == DEMO_AGENT_ROLE:
        return (
            {'error': 'Demo personas cannot be modified through admin endpoints'},
            403,
        )
    return None


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
import os
import re
import requests as http_requests

logger = logging.getLogger(__name__)

# AI agents URL for internal communication (port 8081 for admin/reindex API)
AI_AGENTS_ADMIN_URL = os.getenv('AI_AGENTS_ADMIN_URL', 'http://ai-agents:8081')


# =============================================================================
# Fabric Webhook Sync
# =============================================================================

@admin_bp.route('/fabric/sync-webhooks', methods=['POST'])
@require_auth
def sync_fabric_webhooks():
    """Re-point managed Fabric SWML Webhook resources at the current EXTERNAL_URL.

    Runs automatically at backend startup; this endpoint exposes the same
    operation for on-demand triggering (e.g. after a URL change without a
    container recreate).
    """
    from app.services.fabric_sync import sync_all
    result = sync_all(get_base_url())
    status = 200 if all(
        r.get('ok') or r.get('skipped') for r in result.values()
    ) else 502
    return jsonify(result), status


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
    """List all users for admin management.

    Demo personas (``role='demo_agent'``) are filtered out — they're
    pool fixtures, not human team members. The admin doesn't manage
    them through this view.
    """
    try:
        users = (
            User.query
            .filter(User.role != DEMO_AGENT_ROLE)
            .order_by(User.created_at.desc())
            .all()
        )
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

    refused = _refuse_if_demo_persona(user)
    if refused:
        return jsonify(refused[0]), refused[1]

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


@admin_bp.route('/users/<int:user_id>/languages', methods=['PUT'])
@require_auth
def update_user_languages(user_id):
    """Set the BCP-47 languages this user speaks. Admin-only.

    Used by the routing layer to prefer language-matched agents and to
    decide whether to start live_translate when a call connects.
    """
    data = request.get_json() or {}
    languages = data.get('languages')

    if not isinstance(languages, list) or not all(isinstance(l, str) and l for l in languages):
        return jsonify({'error': 'languages must be a non-empty list of BCP-47 strings'}), 400
    if not languages:
        return jsonify({'error': 'languages cannot be empty — every agent needs at least one'}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    refused = _refuse_if_demo_persona(user)
    if refused:
        return jsonify(refused[0]), refused[1]

    try:
        user.languages = languages
        db.session.commit()
        logger.info(
            f"User {user_id} ({user.email}) languages set to {languages} "
            f"by admin {request.current_user.id}"
        )
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update user languages: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>/permissions', methods=['PUT'])
@require_auth
def update_user_permissions(user_id):
    """Replace a user's per-user permission overrides. Admin-only.

    Body: { "permissions": { "can_listen_ai_calls": true, ... } }

    - Keys MUST be in PERMISSION_FLAGS; unknown keys are rejected.
    - Values MUST be bool.
    - Missing keys = no override for that flag (falls through to role default).
    - Pass `{}` to clear all overrides and fall entirely back to role defaults.
    """
    from app.models.user import PERMISSION_FLAGS

    data = request.get_json() or {}
    permissions = data.get('permissions')

    if not isinstance(permissions, dict):
        return jsonify({'error': 'permissions must be an object'}), 400

    # Reject unknown keys explicitly so a typo doesn't silently set a no-op.
    unknown = [k for k in permissions.keys() if k not in PERMISSION_FLAGS]
    if unknown:
        return jsonify({
            'error': f'unknown permission flags: {unknown}',
            'valid_flags': list(PERMISSION_FLAGS),
        }), 400

    bad_values = [k for k, v in permissions.items() if not isinstance(v, bool)]
    if bad_values:
        return jsonify({
            'error': f'permission values must be bool; bad keys: {bad_values}'
        }), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    refused = _refuse_if_demo_persona(user)
    if refused:
        return jsonify(refused[0]), refused[1]

    try:
        user.permissions = permissions
        db.session.commit()
        logger.info(
            f"User {user_id} ({user.email}) permission overrides set to {permissions} "
            f"by admin {request.current_user.id}"
        )
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update user permissions: {str(e)}")
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

        refused = _refuse_if_demo_persona(user)
        if refused:
            return jsonify(refused[0]), refused[1]

        user_email = user.email

        # Step 1: Delete SignalWire Call Fabric subscriber if one exists.
        # 404 is treated as success — subscriber already gone.
        sw_delete_error = None
        if user.signalwire_subscriber_id and sw_client.is_configured():
            try:
                sw_client.get_client().fabric.subscribers.delete(user.signalwire_subscriber_id)
                logger.info(f"SignalWire subscriber {user.signalwire_subscriber_id} deleted")
            except sw_client.SignalWireRestError as e:
                # 404 means already deleted — fine.
                if getattr(e, 'status_code', None) == 404:
                    logger.info(f"SignalWire subscriber {user.signalwire_subscriber_id} already gone (404)")
                else:
                    sw_delete_error = f"SignalWire subscriber delete failed: {e}"
                    logger.warning(sw_delete_error)
            except Exception as e:
                sw_delete_error = f"Failed to reach SignalWire: {e}"
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
# Phone Number Management
# =============================================================================
# Canonical routing per the SignalWire REST docs (phone-numbers update):
#
#   call_handler='relay_script'  +  call_relay_script_url=<our URL>
#
# `relay_script` is the documented handler for external SWML: "The URL must
# respond with a valid SWML script." SignalWire server-derives a Fabric
# `swml_webhook` resource from the URL (see `calling_handler_resource_id` in
# the response — response-only, not writable).
#
# `laml_webhooks` is deliberately NOT used here — it's Twilio-compat cXML
# and materializes as a `cxml_webhook` resource in Fabric, which is the
# wrong resource type for a call center returning SWML.


@admin_bp.route('/phone-numbers', methods=['GET'])
@require_auth
def list_phone_numbers():
    """List SignalWire phone numbers and flag which are routed to this app."""
    if not sw_client.is_configured():
        return jsonify({'error': 'SignalWire credentials not configured'}), 500

    base_url = get_base_url()
    webhook_url = f"{base_url}/api/swml/initial-call" if base_url else None

    try:
        client = sw_client.get_client()
        resp = client.phone_numbers.list(page_size=100)
        raw_numbers = resp.get('data', []) if isinstance(resp, dict) else resp

        phone_numbers = []
        for n in raw_numbers:
            # relay_script handler stores the URL in call_relay_script_url.
            # Also honor call_request_url so legacy (laml_webhooks) assignments
            # from earlier migrations still show as assigned.
            current_url = n.get('call_relay_script_url') or n.get('call_request_url') or ''
            is_assigned = bool(
                webhook_url
                and current_url
                and current_url.rstrip('/') == webhook_url.rstrip('/')
            )
            phone_numbers.append({
                'sid': n.get('id'),
                'phone_number': n.get('number') or n.get('phone_number', ''),
                'friendly_name': n.get('name') or '',
                'voice_url': current_url,
                'call_handler': n.get('call_handler') or '',
                'status_callback': n.get('call_status_callback_url') or '',
                'is_assigned': is_assigned,
            })

        return jsonify({
            'phone_numbers': phone_numbers,
            'webhook_url': webhook_url or '',
            'is_configured': bool(base_url),
        }), 200

    except sw_client.SignalWireRestError as e:
        logger.error(f"SignalWire API error listing phone numbers: {e}")
        return jsonify({'error': f'SignalWire API error: {e}'}), 502
    except Exception as e:
        logger.error(f"Failed to list phone numbers: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/phone-numbers/<string:number_sid>', methods=['POST'])
@require_auth
def update_phone_number(number_sid):
    """Route/unroute a phone number to the call center's SWML webhook."""
    data = request.get_json() or {}
    action = data.get('action')

    if action not in ('assign', 'unassign'):
        return jsonify({'error': 'action must be "assign" or "unassign"'}), 400

    base_url = get_base_url()
    if action == 'assign' and not base_url:
        return jsonify({'error': 'EXTERNAL_URL not configured. Cannot assign webhook.'}), 400

    webhook_url = f"{base_url}/api/swml/initial-call"
    client = sw_client.get_client()

    try:
        if action == 'assign':
            client.phone_numbers.update(
                number_sid,
                call_handler='relay_script',
                call_relay_script_url=webhook_url,
            )
            logger.info(f"Routed phone {number_sid} to SWML webhook {webhook_url}")
            return jsonify({
                'success': True,
                'phone_number': {'sid': number_sid, 'is_assigned': True},
                'message': 'Phone number routed to call center (SWML)',
            }), 200

        # Unassign — keep the handler as relay_script but clear the URL so
        # no Fabric resource gets associated. Matches the "detached"
        # presentation in the dashboard.
        client.phone_numbers.update(
            number_sid,
            call_handler='relay_script',
            call_relay_script_url='',
        )
        logger.info(f"Unrouted phone {number_sid} (cleared call_relay_script_url)")
        return jsonify({
            'success': True,
            'phone_number': {'sid': number_sid, 'is_assigned': False},
            'message': 'Phone number unrouted from call center',
        }), 200

    except sw_client.SignalWireRestError as e:
        logger.error(f"Failed to update phone number {number_sid}: {e}")
        return jsonify({'error': f'SignalWire API error: {e}'}), 502
    except Exception as e:
        logger.error(f"Failed to update phone number {number_sid}: {e}")
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
