from flask import request, jsonify
from app import db
from app.api import admin_bp
from app.models import Call, Transcription, User
from app.models.system_config import SystemConfig
from app.models.document import DocumentCollection, Document, AgentCollectionAssignment
from app.utils.decorators import require_auth
import logging
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
