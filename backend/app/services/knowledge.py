"""Knowledge-base helpers shared by the Factbook/coach search surfaces."""
import logging

logger = logging.getLogger(__name__)

DEFAULT_KB_COLLECTION = 'sales_knowledge'


def kb_collection_for_queue(queue_slug, workspace_id):
    """Resolve the KB search collection for a queue's calls.

    Chain: queue slug → the queue's AI fallback agent route ('/sales-ai') →
    that agent's AgentCollectionAssignment → the collection's physical name
    (the search key /search builds ``chunks_{name}`` from — see internal.py
    list_agent_assignments). Falls back to :data:`DEFAULT_KB_COLLECTION`
    (the historic default) when any link is missing, so unconfigured queues
    behave as before. Scoped explicitly via workspace_context because some
    callers are public webhooks with no authenticated user (queue slugs are
    only unique per workspace).
    """
    from app.models import AgentCollectionAssignment
    from app.models.queue import Queue
    from app.tenancy import workspace_context

    if not queue_slug or not workspace_id:
        return DEFAULT_KB_COLLECTION
    try:
        with workspace_context(workspace_id):
            queue = Queue.query.filter_by(slug=queue_slug).first()
            agent_slug = ((queue.ai_agent_route or '') if queue else '').strip().strip('/')
            if not agent_slug:
                return DEFAULT_KB_COLLECTION
            assignment = AgentCollectionAssignment.query.filter_by(
                agent_id=agent_slug
            ).first()
            if assignment and assignment.collection:
                return (assignment.collection.physical_name
                        or assignment.collection.name)
    except Exception:
        logger.exception("KB collection lookup failed for queue=%s ws=%s",
                         queue_slug, workspace_id)
    return DEFAULT_KB_COLLECTION
