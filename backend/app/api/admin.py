from flask import request, jsonify, g
from app import db
from app.api import admin_bp
from app.models import Call, Transcription, User, WebhookEvent
from app.models.system_config import SystemConfig
from app.models.document import DocumentCollection, Document, AgentCollectionAssignment
from app.models.queue import Queue, QueueAgentAssignment
from app.utils.decorators import require_auth, require_role
from app.utils.jwt_utils import verify_token
from app.utils.url_utils import get_base_url
from app.services import signalwire_client as sw_client

# Roles an admin is allowed to assign via the User Management UI.
# (The old reserved ``demo_agent`` persona role is gone with the shared
# floor — hosted visitors are ordinary workspace-scoped admins now.)
VALID_USER_ROLES = ('admin', 'supervisor', 'agent')


def _require_platform_admin():
    """403 body for workspace-bound admins on PLATFORM-resource endpoints
    (§9): phone-number routing, fabric webhook sync, demo stats, and
    subscriber provisioning act on install-wide SignalWire state that no
    single tenant may touch. Platform admins (workspace NULL — the
    operator, and every clone-and-own admin) pass. Returns None to allow.
    """
    if getattr(request.current_user, 'workspace_id', None) is None:
        return None
    return (
        {
            'error': 'This is a platform-level operation not available inside a workspace.',
            'code': 'platform_only',
        },
        403,
    )


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
    # Tenancy: workspace admins (hosted visitors) get every admin query
    # auto-scoped to their workspace; platform admins (workspace NULL)
    # stay unscoped. Mirrors require_auth (see app/tenancy.py).
    g.workspace_id = user.workspace_id
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
    platform_only = _require_platform_admin()
    if platform_only:
        return jsonify(platform_only[0]), platform_only[1]

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
# Branding (IMP-02 white-label)
# =============================================================================

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


@admin_bp.route('/branding', methods=['GET'])
@require_auth
def get_branding():
    """Current white-label branding (None values mean stock SignalWire)."""
    return jsonify({'branding': SystemConfig.get_branding_config()}), 200


@admin_bp.route('/branding', methods=['PUT'])
@require_auth
def update_branding():
    """Update white-label branding. Saving an empty string clears a field.

    Applies live: the frontend re-fetches /api/config/runtime after save and
    re-applies CSS variables — no rebuild, no restart.
    """
    try:
        data = request.get_json() or {}
        user_id = request.current_user.id
        updated = []

        for field in SystemConfig.BRANDING_FIELDS:
            if field not in data:
                continue
            value = (data[field] or '').strip()
            if value and field.startswith('color_') and not _HEX_COLOR.match(value):
                return jsonify({'error': f'{field} must be a #rrggbb hex value'}), 400
            if value and field == 'logo_url' and not value.startswith(('https://', 'http://', '/')):
                return jsonify({'error': 'logo_url must be an http(s) URL or absolute path'}), 400
            if field == 'product_name' and len(value) > 60:
                return jsonify({'error': 'product_name must be 60 characters or fewer'}), 400
            SystemConfig.set(f'branding.{field}', value, user_id=user_id)
            updated.append(field)

        return jsonify({
            'success': True,
            'updated': updated,
            'branding': SystemConfig.get_branding_config(),
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update branding: {str(e)}")
        return jsonify({'error': 'Failed to update branding'}), 500


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
        from app.utils.workspace_caps import cap_denial
        capped = cap_denial('queues')
        if capped:
            return jsonify(capped[0]), capped[1]

        data = request.get_json()
        if not data or not data.get('slug') or not data.get('display_name'):
            return jsonify({'error': 'slug and display_name are required'}), 400

        slug = data['slug'].lower().strip()
        if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', slug):
            return jsonify({'error': 'slug must be lowercase alphanumeric with hyphens'}), 400

        if Queue.find_by_slug(slug):
            return jsonify({'error': f'Queue "{slug}" already exists'}), 409

        # Validate routing_transport — only two values, conference (default)
        # or bridge. Anything else falls back to conference so a typo can't
        # break call routing.
        routing_transport = data.get('routing_transport', 'conference')
        if routing_transport not in ('conference', 'bridge'):
            return jsonify({
                'error': "routing_transport must be 'conference' or 'bridge'",
            }), 400
        # Hosted mode: bridge transport parks callers in SignalWire's NATIVE
        # queue named by the bare slug — shared across every workspace, so
        # one workspace's pickup would pop another's caller. Blocked until
        # Phase 4's telephony re-key names native queues per workspace.
        from app.utils.demo_config import tenancy_mode_active
        if routing_transport == 'bridge' and tenancy_mode_active():
            return jsonify({
                'error': 'Bridge transport is not available in hosted mode yet.',
                'code': 'demo_blocked',
            }), 400

        queue = Queue(
            slug=slug,
            display_name=data['display_name'],
            description=data.get('description'),
            routing_strategy=data.get('routing_strategy', 'round_robin'),
            routing_transport=routing_transport,
            ai_agent_route=data.get('ai_agent_route'),
            default_priority=data.get('default_priority', 5),
            sla_threshold_seconds=data.get('sla_threshold_seconds', 60),
            max_wait_before_ai_fallback=data.get('max_wait_before_ai_fallback', 120),
        )
        db.session.add(queue)
        db.session.commit()

        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_config_changed', {'action': 'created', 'queue': queue.to_dict()},
                      room=workspace_room(queue.workspace_id))

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

        # Validate routing_transport if present — only two valid values.
        if 'routing_transport' in data and data['routing_transport'] not in (
            'conference', 'bridge',
        ):
            return jsonify({
                'error': "routing_transport must be 'conference' or 'bridge'",
            }), 400
        # Hosted mode: bridge transport blocked until Phase 4 names the
        # NATIVE SignalWire queues per workspace (bare slug names are
        # shared install-wide — cross-workspace caller pops).
        from app.utils.demo_config import tenancy_mode_active
        if data.get('routing_transport') == 'bridge' and tenancy_mode_active():
            return jsonify({
                'error': 'Bridge transport is not available in hosted mode yet.',
                'code': 'demo_blocked',
            }), 400

        for field in ['display_name', 'description', 'is_active', 'routing_strategy',
                      'routing_transport', 'ai_agent_route', 'default_priority',
                      'sla_threshold_seconds', 'max_wait_before_ai_fallback']:
            if field in data:
                setattr(queue, field, data[field])

        db.session.commit()

        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_config_changed', {'action': 'updated', 'queue': queue.to_dict()},
                      room=workspace_room(queue.workspace_id))

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
        queue_ws_id = queue.workspace_id  # capture before the row is gone
        db.session.delete(queue)
        db.session.commit()

        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_config_changed', {'action': 'deleted', 'queue_slug': slug},
                      room=workspace_room(queue_ws_id))

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
        from app.utils.workspace_caps import cap_denial
        capped = cap_denial('collections')
        if capped:
            return jsonify(capped[0]), capped[1]

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
        from app.utils.workspace_caps import cap_denial
        capped = cap_denial('documents')
        if capped:
            return jsonify(capped[0]), capped[1]

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

        # Prepare payload for AI agents reindex endpoint.
        # Tenancy: the chunk-table identity is physical_name (ws{ID}_{name}
        # for clones, = name for the default workspace's migrated rows) —
        # NEVER the per-workspace display name, or every workspace's
        # "sales_knowledge" reindex would clobber the same chunks_ table.
        payload = {
            'collection_name': collection.physical_name or collection.name,
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

        # Return updated assignments. Agents resolve their KB binding per
        # request from a 30s-TTL cache (capture_base_url → attach_knowledge_search
        # in ai-agents/main_agent.py) — no restart needed.
        assignments = AgentCollectionAssignment.query.all()
        return jsonify({
            'success': True,
            'assignments': [a.to_dict() for a in assignments],
            'message': 'Saved — agents pick up knowledge base changes on new calls within 30 seconds',
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update agent assignments: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# User Management
# =============================================================================

@admin_bp.route('/users', methods=['POST'])
@require_auth
def create_user():
    """Create a team member (Phase 2 — the invite-a-colleague demo).

    A workspace admin creates users INSIDE their workspace (the flush
    stamper takes g.workspace_id, so the row lands scoped); a platform
    admin's creations stay platform-level, matching clone-and-own's
    user model. Hosted-mode colleagues exist for routing/wallboard
    realism and to demo the surface — they cannot password-login
    (hosted login only matches platform users), and if no password is
    supplied the account gets an unusable random one.
    """
    try:
        from app.utils.workspace_caps import cap_denial
        capped = cap_denial('users')
        if capped:
            return jsonify(capped[0]), capped[1]

        data = request.get_json() or {}
        email = (data.get('email') or '').strip().lower()
        name = (data.get('name') or '').strip()
        role = (data.get('role') or 'agent').strip()

        if not email or '@' not in email:
            return jsonify({'error': 'A valid email is required'}), 400
        if not name:
            return jsonify({'error': 'name is required'}), 400
        if role not in VALID_USER_ROLES:
            return jsonify({
                'error': f'role must be one of: {", ".join(VALID_USER_ROLES)}'
            }), 400

        # Per-workspace uniqueness (COALESCE(workspace_id,0)+email index):
        # this lookup runs auto-scoped, so it checks exactly the right layer
        # for both workspace and platform admins.
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'A user with this email already exists'}), 409

        password = data.get('password')
        if password is not None and len(str(password)) < 12:
            return jsonify({'error': 'password must be at least 12 characters'}), 400

        import secrets as _secrets
        user = User(
            email=email,
            name=name,
            role=role,
            is_active=True,
            languages=data.get('languages') or ['en-US'],
            permissions={},
        )
        user.set_password(str(password) if password else _secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.commit()

        return jsonify({'success': True, 'user': user.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create user: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users', methods=['GET'])
@require_auth
def list_users():
    """List users for admin management.

    Auto-scoped by tenancy: a workspace admin sees their workspace's
    members; platform admins (workspace NULL) see everyone.
    """
    try:
        users = (
            User.query
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


KB_FACTBOOK_MODES = {'off', 'manual', 'auto'}
COACH_MODES = {'off', 'on_request', 'auto'}
COACH_INTENSITIES = {'terse', 'standard', 'verbose'}


@admin_bp.route('/users/<int:user_id>/kb-factbook-mode', methods=['PUT'])
@require_auth
def update_user_kb_factbook_mode(user_id):
    """Set this user's Knowledge Factbook mode for Agent Assist. Admin-only."""
    data = request.get_json() or {}
    mode = data.get('kb_factbook_mode')

    if mode not in KB_FACTBOOK_MODES:
        return jsonify({
            'error': f'kb_factbook_mode must be one of: {", ".join(sorted(KB_FACTBOOK_MODES))}'
        }), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404


    try:
        user.kb_factbook_mode = mode
        db.session.commit()
        logger.info(
            f"User {user_id} ({user.email}) kb_factbook_mode set to '{mode}' "
            f"by admin {request.current_user.id}"
        )
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update kb_factbook_mode: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>/coach-settings', methods=['PUT'])
@require_auth
def update_user_coach_settings(user_id):
    """Set this user's AI Coach style preset. Admin-only.

    Body: ``{coach_intensity: terse|standard|verbose}``.

    Note: ``coach_mode`` is no longer admin-controlled — agents pick mode
    per-call in the live Coach panel (gated by the ``can_use_coach``
    permission flag). This endpoint now only writes the agent's style
    preset, which the sidecar prompt uses at attach time.
    """
    data = request.get_json() or {}
    intensity = data.get('coach_intensity')

    # coach_mode is intentionally not accepted here. If a stale client sends
    # it, ignore rather than error so the call doesn't fail outright.
    if 'coach_mode' in data:
        logger.info(
            f"coach-settings: ignoring deprecated coach_mode field for user "
            f"{user_id} — mode is now an in-call agent toggle."
        )

    if intensity is None:
        return jsonify({'error': 'coach_intensity is required'}), 400
    if intensity not in COACH_INTENSITIES:
        return jsonify({
            'error': f'coach_intensity must be one of: {", ".join(sorted(COACH_INTENSITIES))}'
        }), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404


    try:
        user.coach_intensity = intensity
        db.session.commit()
        logger.info(
            f"User {user_id} ({user.email}) coach_intensity set to "
            f"'{intensity}' by admin {request.current_user.id}"
        )
        return jsonify({'user': user.to_dict()}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update coach settings: {str(e)}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>/reset-subscriber', methods=['POST'])
@require_auth
def reset_user_subscriber(user_id):
    """Force-recreate this user's SignalWire Call Fabric subscriber.

    Recovery hammer for the "WebRTC endpoint registration failed" (-32603)
    class of bugs originating in Hagrid's `mWebRTCEndpoints` legacy
    registration cache (see CALL_TRANSPORT.md changelog 2026-05-14). When a
    subscriber's device binding gets stuck server-side, the SDK can't
    register a new device for that subscriber. Deleting the subscriber and
    minting a fresh one drops all stale bindings.

    Flow:
      1. Best-effort DELETE the SignalWire subscriber via Fabric REST API.
         404 is treated as success (already gone).
      2. NULL out the user's signalwire_* fields locally. Next call to
         /api/fabric/token will trigger _create_permanent_subscriber, which
         mints a brand-new subscriber with a fresh subscriber_id, username,
         and password — no inherited Hagrid binding state.
      3. The user must reload their browser to pick up the new subscriber.
         If they're resetting their own (admin), the frontend chains a
         localStorage clear + reload after the response.

    Admin-only. Refuses on demo personas (they auto-recreate via demo_seed).
    """
    platform_only = _require_platform_admin()
    if platform_only:
        return jsonify(platform_only[0]), platform_only[1]

    # Reusing the role gate pattern from other admin endpoints — only
    # admins can wipe subscribers, including their own.
    if getattr(request.current_user, 'role', None) != 'admin':
        return jsonify({'error': 'Admin role required'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404


    sw_delete_error = None
    deleted_subscriber_id = user.signalwire_subscriber_id

    # Step 1: try to delete the SignalWire subscriber. Best-effort — even
    # if SignalWire is unreachable, we still clear the local fields so the
    # user can mint a fresh subscriber on next token request.
    if deleted_subscriber_id and sw_client.is_configured():
        try:
            sw_client.get_client().fabric.subscribers.delete(deleted_subscriber_id)
            logger.info(
                f"reset-subscriber: SignalWire subscriber "
                f"{deleted_subscriber_id} deleted for user {user_id}"
            )
        except sw_client.SignalWireRestError as e:
            if getattr(e, 'status_code', None) == 404:
                logger.info(
                    f"reset-subscriber: SW subscriber {deleted_subscriber_id} "
                    f"already gone (404) — treating as success"
                )
            else:
                sw_delete_error = f"SignalWire delete failed: {e}"
                logger.warning(f"reset-subscriber: {sw_delete_error}")
        except Exception as e:
            sw_delete_error = f"SignalWire unreachable: {e}"
            logger.warning(f"reset-subscriber: {sw_delete_error}")

    # Step 2: null the local fields. About to recreate, so this is just
    # the gate that lets _create_permanent_subscriber take the
    # "no existing subscriber" branch in step 3.
    try:
        user.signalwire_subscriber_id = None
        user.signalwire_username = None
        user.signalwire_password_encrypted = None
        user.signalwire_address = None
        user.fabric_subscriber_created_at = None
        db.session.commit()
        logger.info(
            f"reset-subscriber: cleared local subscriber fields for "
            f"user {user_id} ({user.email}) by admin {request.current_user.id}"
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"reset-subscriber: local field clear failed: {e}")
        return jsonify({
            'error': 'Failed to clear local subscriber state',
            'detail': str(e),
            'sw_warning': sw_delete_error,
        }), 500

    # Step 3: mint a fresh subscriber immediately. Atomic from the caller's
    # POV — they go in with a stale subscriber, come out with a working one.
    # If this step fails, the user is left in the "no subscriber yet"
    # transient state — they can still sign in to retry (the token endpoint
    # will _create_permanent_subscriber on first call). We surface the
    # failure as `recreate_error` so the UI can warn.
    sw_recreate_error = None
    new_subscriber_id = None
    try:
        from app.api.fabric import _create_permanent_subscriber
        _create_permanent_subscriber(user)
        new_subscriber_id = user.signalwire_subscriber_id
        logger.info(
            f"reset-subscriber: fresh SW subscriber minted for user {user_id}: "
            f"{new_subscriber_id} ({user.signalwire_address})"
        )
    except Exception as e:
        sw_recreate_error = f"Subscriber recreate failed: {e}"
        logger.error(f"reset-subscriber: {sw_recreate_error}", exc_info=True)
        # Don't fail the whole request — the delete + clear succeeded, so
        # the user is unblocked from the stuck-binding state even if the
        # immediate recreate didn't go through. They can retry by signing
        # back in (which calls /api/fabric/token → _create_permanent_subscriber).

    result = {
        'success': True,
        'message': (
            f'Subscriber reset for {user.email}.'
            + (
                ' Fresh subscriber created — reload to pick it up.'
                if new_subscriber_id
                else ' Recreate failed; sign in again to retry.'
            )
        ),
        'deleted_subscriber_id': deleted_subscriber_id,
        'new_subscriber_id': new_subscriber_id,
        'user': user.to_dict(),
    }
    if sw_delete_error:
        # Soft warning — local state IS cleared, SignalWire-side may have
        # the orphaned record. It'll TTL out or be cleared on Hagrid restart.
        result['sw_warning'] = sw_delete_error
    if sw_recreate_error:
        result['recreate_error'] = sw_recreate_error

    return jsonify(result), 200


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

VALID_PHONE_TARGET_MODES = ('ai_triage', 'ai_specialist', 'human_direct')


def _compute_phone_routing_url(base_url: str, target_mode: str, target_queue_slug):
    """Pick the SWML webhook URL for a phone number based on its routing mode.
    Raises ValueError for invalid combinations (queue-mode missing a queue)."""
    if target_mode == 'ai_specialist':
        if not target_queue_slug:
            raise ValueError("target_queue_slug required for ai_specialist mode")
        return f"{base_url}/api/swml/ai-specialist/{target_queue_slug}"
    if target_mode == 'human_direct':
        if not target_queue_slug:
            raise ValueError("target_queue_slug required for human_direct mode")
        return f"{base_url}/api/queues/{target_queue_slug}/direct-inbound"
    return f"{base_url}/api/swml/initial-call"


def _parse_phone_routing_from_url(url, base_url):
    """Reverse of _compute_phone_routing_url. Returns
    ``{'target_mode', 'target_queue_slug'}`` or None when the URL isn't one
    we manage (numbers pointing at an external SWML / cXML script).

    Strict mode — only matches when ``url`` starts with the CURRENT
    ``base_url``. Use this for "is this number assigned to us right now"
    decisions. URL drift (ngrok rotation, dev↔prod host change) makes the
    same logical assignment fail this check. Pair with
    :func:`_parse_phone_routing_loose` to recover the routing intent.
    """
    if not url or not base_url:
        return None
    base_trim = base_url.rstrip('/')
    if not url.startswith(base_trim):
        return None
    return _parse_phone_routing_loose(url)


def _parse_phone_routing_loose(url):
    """Host-agnostic variant of :func:`_parse_phone_routing_from_url`.

    Extracts our routing intent from the PATH alone, regardless of which
    host the URL points at. Used for URL-drift detection — if a phone
    number on SignalWire's side has a URL whose path matches one of our
    known SWML routes but whose host doesn't match the current
    ``base_url``, this returns the mode/queue we'd re-bind to, and the
    caller flags the row as drifted.

    Returns None when the path doesn't look like one of our routes (true
    external assignment, e.g. a customer pointing at their own script).
    """
    if not url:
        return None
    # Drop scheme + host so we can match on path alone.
    path = url
    for prefix in ('https://', 'http://'):
        if path.startswith(prefix):
            rest = path[len(prefix):]
            slash = rest.find('/')
            path = rest[slash:] if slash >= 0 else '/'
            break
    if '?' in path:
        path = path.split('?', 1)[0]
    path = path.rstrip('/')

    if path == '/api/swml/initial-call':
        return {'target_mode': 'ai_triage', 'target_queue_slug': None}
    if path.startswith('/api/swml/ai-specialist/'):
        slug = path[len('/api/swml/ai-specialist/'):]
        if slug:
            return {'target_mode': 'ai_specialist', 'target_queue_slug': slug}
    if path.startswith('/api/queues/') and path.endswith('/direct-inbound'):
        slug = path[len('/api/queues/'):-len('/direct-inbound')]
        if slug:
            return {'target_mode': 'human_direct', 'target_queue_slug': slug}
    return None


@admin_bp.route('/phone-numbers', methods=['GET'])
@require_auth
def list_phone_numbers():
    """List SignalWire phone numbers and flag which are routed to this app.

    §6.4 split: WORKSPACE admins get a read-only view — the shared entry
    numbers (the landing-card list) + their own verified-number status,
    never the install's real DIDs/SIDs (no re-route surface; the POST
    below stays platform-only). Platform admins (workspace NULL) keep the
    full management tab.
    """
    if request.current_user.workspace_id is not None:
        from app.services.demo_verify import verify_status
        from app.utils.demo_config import demo_phone_numbers
        vs = verify_status(request.current_user.workspace_id)
        return jsonify({
            'read_only': True,
            'phone_numbers': [],
            'webhook_url': '',
            'is_configured': True,
            'entry_numbers': demo_phone_numbers(),
            'verified': vs.get('verified', False),
            'verified_masked': vs.get('masked_number'),
        })

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

            # Strict parse: is this number currently routed to us at the
            # CURRENT base_url?
            routing = _parse_phone_routing_from_url(current_url, base_url) if base_url else None

            # Loose parse: does the URL's PATH look like one of our SWML
            # routes regardless of host? If yes but strict failed, the
            # number was assigned to us under a previous base_url (ngrok
            # rotation, dev↔prod move, etc.) and is now drifted. We can
            # also recover the original routing intent so the UI offers a
            # one-click re-sync.
            loose = _parse_phone_routing_loose(current_url) if current_url else None
            is_drifted = bool(loose) and routing is None

            phone_numbers.append({
                'sid': n.get('id'),
                'phone_number': n.get('number') or n.get('phone_number', ''),
                'friendly_name': n.get('name') or '',
                'voice_url': current_url,
                'call_handler': n.get('call_handler') or '',
                'status_callback': n.get('call_status_callback_url') or '',
                'is_assigned': routing is not None,
                'target_mode': routing['target_mode'] if routing else None,
                'target_queue_slug': routing['target_queue_slug'] if routing else None,
                # Drift fields — populated only when the number is pointed
                # at one of our routes under a non-current host. The UI
                # shows a distinct "URL drifted" chip and a Re-sync button
                # that re-binds to (drifted_target_mode, drifted_target_queue_slug).
                'is_drifted': is_drifted,
                'drifted_target_mode': loose['target_mode'] if is_drifted else None,
                'drifted_target_queue_slug': loose['target_queue_slug'] if is_drifted else None,
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
    """Route/unroute a phone number to a specific SWML entry point.

    Body for ``assign``:
      ``{action: 'assign', target_mode?: str, target_queue_slug?: str}``
      Defaults: ``target_mode='ai_triage'``, no queue.

    Body for ``unassign``: ``{action: 'unassign'}``.
    """
    platform_only = _require_platform_admin()
    if platform_only:
        return jsonify(platform_only[0]), platform_only[1]

    data = request.get_json() or {}
    action = data.get('action')

    if action not in ('assign', 'unassign'):
        return jsonify({'error': 'action must be "assign" or "unassign"'}), 400

    base_url = get_base_url()
    # Both assign and unassign now require a base_url. Assign points at a
    # routing SWML; unassign points at /api/swml/out-of-service (SignalWire
    # rejects empty call_relay_script_url with 422).
    if not base_url:
        return jsonify({'error': 'EXTERNAL_URL not configured. Cannot update phone number routing.'}), 400

    client = sw_client.get_client()

    try:
        if action == 'assign':
            target_mode = data.get('target_mode') or 'ai_triage'
            target_queue_slug = data.get('target_queue_slug')

            if target_mode not in VALID_PHONE_TARGET_MODES:
                return jsonify({
                    'error': f"target_mode must be one of: {', '.join(VALID_PHONE_TARGET_MODES)}"
                }), 400

            if target_mode in ('ai_specialist', 'human_direct'):
                if not target_queue_slug:
                    return jsonify({
                        'error': f"target_queue_slug is required for target_mode='{target_mode}'"
                    }), 400
                queue = Queue.query.filter_by(slug=target_queue_slug, is_active=True).first()
                if not queue:
                    return jsonify({
                        'error': f"Queue '{target_queue_slug}' not found or inactive"
                    }), 400
                if target_mode == 'ai_specialist' and not queue.ai_agent_route:
                    return jsonify({
                        'error': (
                            f"Queue '{target_queue_slug}' has no AI agent route configured. "
                            f"Pick another queue or use human_direct mode."
                        )
                    }), 400

            try:
                webhook_url = _compute_phone_routing_url(base_url, target_mode, target_queue_slug)
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400

            from app.utils.url_utils import signed_webhook_url
            from app.services.fabric_sync import update_swml_webhook_for_phone
            status_callback_url = signed_webhook_url(
                f"{base_url}/api/webhooks/call-status"
            )
            updated = client.phone_numbers.update(
                number_sid,
                call_handler='relay_script',
                call_relay_script_url=webhook_url,
                call_status_callback_url=status_callback_url,
            )

            # phone_numbers.update keeps the legacy fields aligned but the
            # field SignalWire's runtime actually reads for status callbacks
            # lives on the swml_webhook Fabric resource (the Dashboard's
            # "Calling Handler"). The legacy call_status_callback_url is
            # display-only for relay_script handlers; without explicitly
            # updating the swml_webhook here, the status_callback_url stays
            # at whatever it was when the resource was first created (often
            # a stale ngrok subdomain from a prior session).
            handler_id = (updated or {}).get('calling_handler_resource_id')
            swml_wh_result = update_swml_webhook_for_phone(
                calling_handler_resource_id=handler_id,
                primary_request_url=webhook_url,
                status_callback_url=status_callback_url,
            )
            logger.info(
                f"Routed phone {number_sid} → mode={target_mode} "
                f"queue={target_queue_slug or '—'} url={webhook_url} "
                f"status_callback={status_callback_url} "
                f"swml_webhook={handler_id} → {swml_wh_result.get('ok')}"
            )
            return jsonify({
                'success': True,
                'phone_number': {
                    'sid': number_sid,
                    'is_assigned': True,
                    'target_mode': target_mode,
                    'target_queue_slug': target_queue_slug,
                    'voice_url': webhook_url,
                },
                'message': 'Phone number routed',
            }), 200

        # Unassign — SignalWire rejects empty call_relay_script_url with
        # 422 ("Call relay script url must be set"). Point instead at a
        # dedicated "out of service" SWML endpoint that plays a rejection
        # message and hangs up. _parse_phone_routing_from_url doesn't match
        # this path, so the number correctly reads as is_assigned=False in
        # the admin UI.
        oos_url = f"{base_url}/api/swml/out-of-service"
        from app.utils.url_utils import signed_webhook_url
        from app.services.fabric_sync import update_swml_webhook_for_phone
        status_callback_url = signed_webhook_url(
            f"{base_url}/api/webhooks/call-status"
        )
        updated = client.phone_numbers.update(
            number_sid,
            call_handler='relay_script',
            call_relay_script_url=oos_url,
            call_status_callback_url=status_callback_url,
        )
        # Also sync the swml_webhook (the real source of truth — see assign
        # branch above for the long comment).
        handler_id = (updated or {}).get('calling_handler_resource_id')
        update_swml_webhook_for_phone(
            calling_handler_resource_id=handler_id,
            primary_request_url=oos_url,
            status_callback_url=status_callback_url,
        )
        logger.info(f"Unrouted phone {number_sid} → out-of-service SWML")
        return jsonify({
            'success': True,
            'phone_number': {
                'sid': number_sid,
                'is_assigned': False,
                'target_mode': None,
                'target_queue_slug': None,
            },
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


# =============================================================================
# Webhook Event Log (Tier 2i)
# =============================================================================

# Soft cap on page size to keep payload small — UI paginates on top of this.
_WEBHOOK_PAGE_LIMIT_DEFAULT = 50
_WEBHOOK_PAGE_LIMIT_MAX = 200


@admin_bp.route('/webhook-events', methods=['GET'])
def list_webhook_events():
    """List webhook events with optional filtering.

    Query params:
        event_type: Substring match against event_type (case-insensitive).
        call_id:    Filter to a specific Call.id.
        processed:  'true' / 'false' to filter by processed flag.
        page:       1-indexed page number (default 1).
        per_page:   Items per page, capped at _WEBHOOK_PAGE_LIMIT_MAX.

    Returns total + page metadata so the UI can paginate. The events
    are returned newest-first to match how a developer would scan a
    log in real time.
    """
    event_type_filter = (request.args.get('event_type') or '').strip()
    call_id_filter = request.args.get('call_id', type=int)
    processed_arg = request.args.get('processed')
    page = max(1, request.args.get('page', default=1, type=int) or 1)
    per_page = min(
        _WEBHOOK_PAGE_LIMIT_MAX,
        max(1, request.args.get('per_page', default=_WEBHOOK_PAGE_LIMIT_DEFAULT, type=int) or _WEBHOOK_PAGE_LIMIT_DEFAULT),
    )

    query = db.session.query(WebhookEvent)

    if event_type_filter:
        query = query.filter(WebhookEvent.event_type.ilike(f"%{event_type_filter}%"))
    if call_id_filter:
        query = query.filter(WebhookEvent.call_id == call_id_filter)
    if processed_arg in ('true', 'false'):
        query = query.filter(WebhookEvent.processed == (processed_arg == 'true'))

    total = query.count()
    events = (
        query.order_by(WebhookEvent.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return jsonify({
        'events': [e.to_dict() for e in events],
        'page': page,
        'per_page': per_page,
        'total': total,
        'has_more': (page * per_page) < total,
    }), 200


@admin_bp.route('/webhook-events/event-types', methods=['GET'])
def list_webhook_event_types():
    """Return distinct event_type values for the dropdown filter.

    Capped at 200 to bound the response — if a deployment ever sees
    more distinct types than that, the UI's free-text filter still works.
    """
    rows = (
        db.session.query(WebhookEvent.event_type)
        .distinct()
        .order_by(WebhookEvent.event_type.asc())
        .limit(200)
        .all()
    )
    return jsonify({'event_types': [r[0] for r in rows if r[0]]}), 200


@admin_bp.route('/demo/stats', methods=['GET'])
def demo_stats():
    """Hosted-demo health for the real admin (M6 telemetry, tenancy cut).

    Post-tenancy the scarce resource is the subscriber SEAT pool and the
    unit of occupancy is a live WORKSPACE — the "are we ever at the cap?"
    question. Response keeps the old key names (pool_size/provisioned/
    active_leases) so the admin UI needs no change; a `workspaces` block
    carries the new numbers. Works (harmlessly) on clone-and-own too:
    demo_mode false, zero visitor workspaces.
    """
    platform_only = _require_platform_admin()
    if platform_only:
        return jsonify(platform_only[0]), platform_only[1]

    from app.models import SubscriberSeat, Workspace
    from app.tenancy import DEFAULT_WORKSPACE_ID, workspace_context
    from app.utils.demo_config import is_demo_mode

    with workspace_context(None):
        pool_size = SubscriberSeat.query.count()
        provisioned = SubscriberSeat.query.filter(
            SubscriberSeat.signalwire_subscriber_id.isnot(None)).count()
        leased = SubscriberSeat.query.filter(
            SubscriberSeat.leased_by_user_id.isnot(None)).count()
        active_workspaces = (
            Workspace.query
            .filter(Workspace.status == Workspace.STATUS_ACTIVE)
            .filter(Workspace.id != DEFAULT_WORKSPACE_ID)
            .count()
        )

    return jsonify({
        'demo_mode': is_demo_mode(),
        'pool_size': pool_size,
        'provisioned': provisioned,
        'active_leases': leased,
        'workspaces': {
            'active': active_workspaces,
        },
    }), 200
