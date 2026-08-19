"""
Queue Management Service for Call Center
Handles call queuing, agent availability, and call distribution
"""

from typing import Optional, List, Dict, Any
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
import redis

logger = logging.getLogger(__name__)


@dataclass
class QueuedCall:
    """Represents a call in queue"""
    call_id: str
    queue_id: str
    priority: int
    context: Dict[str, Any]
    enqueued_at: datetime
    caller_number: str
    caller_name: Optional[str] = None


class QueueService:
    """Service for managing call queues and agent availability.

    Tenancy (§8.2): queue state is keyed by SLUG, and slugs repeat across
    workspaces (every visitor gets sales/support/billing clones), so the
    slug-keyed keys — the parked-call zset, the agent-activation set and
    the round-robin cursor — carry a ``ws:{workspace_id}:`` prefix.
    Construct with the workspace that owns the queues being touched
    (usually ``call.workspace_id`` or ``queue.workspace_id``); ``None``
    resolves to the default workspace, which in clone-and-own IS the
    deployment. Agent-status keys (``agent:{id}``, ``agents:{status}``,
    ``agent_last_assigned:{id}``), decline cooldowns and the call-sid data
    key stay unprefixed — their identifiers are globally unique.
    """

    def __init__(self, redis_client: redis.Redis, workspace_id=None):
        self.redis = redis_client
        self.workspace_id = workspace_id
        self.queue_prefix = "queue:"
        self.agent_prefix = "agent:"
        self.call_prefix = "call:"

    def _ws_queue_key(self, queue_id: str, workspace_id=None) -> str:
        from app.services.ws_rooms import ws_key
        return ws_key(
            workspace_id if workspace_id is not None else self.workspace_id,
            f"{self.queue_prefix}{queue_id}",
        )

    def _ws_agents_key(self, queue_slug: str, workspace_id=None) -> str:
        from app.services.ws_rooms import ws_key
        return ws_key(
            workspace_id if workspace_id is not None else self.workspace_id,
            f"queue_agents:{queue_slug}",
        )

    def _ws_rr_key(self, queue_slug: str) -> str:
        from app.services.ws_rooms import ws_key
        return ws_key(self.workspace_id, f"round_robin:{queue_slug}")

    def enqueue_call(
        self,
        call_id: str,
        queue_id: str,
        priority: int = 5,
        context: Optional[Dict[str, Any]] = None,
        caller_info: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Add a call to the specified queue

        Args:
            call_id: Unique identifier for the call
            queue_id: Queue to add the call to (sales, support, billing)
            priority: Priority level (1-10, higher = more urgent)
            context: Additional context from AI agents
            caller_info: Caller information (number, name, etc.)

        Returns:
            Queue position and estimated wait time
        """
        queue_key = self._ws_queue_key(queue_id)

        # Remove any existing entry for this call (prevents duplicates from hold loop retries)
        self._remove_call_from_set(queue_key, call_id)

        # LIFE-06 fix (2026-06-02 audit): SLA / wait-time clock must
        # continue from the caller's ORIGINAL arrival, not reset on each
        # enqueue. Previously ``enqueued_at`` was stamped with now() on
        # every enqueue, which meant: (a) return-to-queue (Tier 2p)
        # restarted the wait clock — invisible to the caller but it
        # silently inflated reported service level; (b) the hold-loop
        # re-enqueue path would also reset it on retries. We now prefer
        # ``Call.created_at`` (the moment SignalWire's call-status webhook
        # first saw the call), falling back to now() only when there's
        # no DB row yet (rare, e.g. orphaned re-enqueues). Keep an
        # ``originally_received_at`` distinct from ``enqueued_at`` so
        # queue-position semantics (how long has this call been in THIS
        # queue) can still read the latter if anyone wants — today
        # nothing keys off it, all wait_time math here keys off the new
        # field via _wait_seconds() below.
        original_received_at = datetime.utcnow()
        try:
            from app.models import Call
            existing = Call.find_by_sid(call_id) if call_id else None
            if existing and existing.created_at:
                original_received_at = existing.created_at
        except Exception:
            # Don't fail enqueue on a DB lookup blip — the now() fallback
            # is graceful degradation, not a correctness break.
            pass

        # Create call data
        call_data = {
            "call_id": call_id,
            "queue_id": queue_id,
            "priority": priority,
            "context": context or {},
            "caller_info": caller_info or {},
            # ``enqueued_at`` now means "first received in our system"
            # (preserved across re-enqueues / returns-to-queue); the SLA
            # clock keys off this. _calculate_service_level on the DB
            # side already keys off Call.created_at, so the two clocks
            # now agree.
            "enqueued_at": original_received_at.isoformat(),
        }

        # Calculate score (higher priority = lower score for ZRANGE)
        # Use negative priority and timestamp to ensure FIFO within same priority
        timestamp = datetime.utcnow().timestamp()
        score = (10 - priority) * 1000000 + timestamp

        # Add to sorted set
        self.redis.zadd(queue_key, {json.dumps(call_data): score})

        # Store call data separately for quick access
        call_key = f"{self.call_prefix}{call_id}"
        self.redis.setex(call_key, 3600, json.dumps(call_data))  # Expire after 1 hour

        # Get queue position and estimate wait time
        position = self._get_queue_position(queue_id, call_id)
        estimated_wait = self._estimate_wait_time(queue_id, position)

        # Log queue event
        logger.info(f"Call {call_id} enqueued to {queue_id} with priority {priority}")

        return {
            "queue_id": queue_id,
            "position": position,
            "estimated_wait_seconds": estimated_wait,
            "queue_depth": self.get_queue_depth(queue_id)
        }

    def dequeue_call(self, queue_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the next call from queue for an agent

        Args:
            queue_id: Queue to dequeue from
            agent_id: Agent requesting the call

        Returns:
            Call data if available, None otherwise
        """
        queue_key = self._ws_queue_key(queue_id)

        # Get highest priority call (lowest score)
        calls = self.redis.zrange(queue_key, 0, 0)

        if not calls:
            logger.info(f"No calls in queue {queue_id}")
            return None

        call_data_str = calls[0]
        call_data = json.loads(call_data_str)

        # Remove from queue
        self.redis.zrem(queue_key, call_data_str)

        # Update agent status
        self.set_agent_status(agent_id, "busy", call_data["call_id"])

        # Calculate wait time
        enqueued_at = datetime.fromisoformat(call_data["enqueued_at"])
        wait_time = (datetime.utcnow() - enqueued_at).total_seconds()
        call_data["wait_time_seconds"] = wait_time

        # Log dequeue event
        logger.info(
            f"Call {call_data['call_id']} dequeued from {queue_id} "
            f"by agent {agent_id} after {wait_time:.1f} seconds"
        )

        return call_data

    def get_queue_status(self, queue_id: str) -> Dict[str, Any]:
        """
        Get current status of a queue

        Args:
            queue_id: Queue to check

        Returns:
            Queue statistics
        """
        queue_key = self._ws_queue_key(queue_id)

        # Get all calls in queue
        calls = self.redis.zrange(queue_key, 0, -1, withscores=True)

        if not calls:
            return {
                "queue_id": queue_id,
                "depth": 0,
                "average_wait_seconds": 0,
                "longest_wait_seconds": 0,
                "calls": []
            }

        # Calculate statistics
        now = datetime.utcnow()
        wait_times = []
        call_details = []

        for call_str, score in calls:
            call_data = json.loads(call_str)
            enqueued_at = datetime.fromisoformat(call_data["enqueued_at"])
            wait_time = (now - enqueued_at).total_seconds()
            wait_times.append(wait_time)

            call_details.append({
                "call_id": call_data["call_id"],
                "priority": call_data["priority"],
                "wait_time_seconds": wait_time,
                "caller_name": call_data.get("caller_info", {}).get("name")
            })

        return {
            "queue_id": queue_id,
            "depth": len(calls),
            "average_wait_seconds": sum(wait_times) / len(wait_times),
            "longest_wait_seconds": max(wait_times),
            "calls": call_details
        }

    def get_queue_depth(self, queue_id: str) -> int:
        """Get the number of calls in queue"""
        queue_key = self._ws_queue_key(queue_id)
        return self.redis.zcard(queue_key)

    def set_agent_status(
        self,
        agent_id: str,
        status: str,
        current_call_id: Optional[str] = None
    ) -> None:
        """
        Update agent status

        Args:
            agent_id: Agent identifier
            status: New status (available, busy, break, offline)
            current_call_id: Current call if busy
        """
        agent_key = f"{self.agent_prefix}{agent_id}"

        # Detect transition into 'available' so we can push-dispatch a waiting
        # call instead of waiting for the next /route hold-loop iteration to
        # poll for agents (which adds up to ~30s of avoidable lag).
        previous_status: Optional[str] = None
        existing = self.get_agent_status(agent_id)
        if existing:
            previous_status = existing.get('status')

        agent_data = {
            "agent_id": agent_id,
            "status": status,
            "current_call_id": current_call_id,
            "last_status_change": datetime.utcnow().isoformat()
        }

        self.redis.setex(agent_key, 28800, json.dumps(agent_data))  # Expire after 8 hours

        # Update agent set for the status
        status_key = f"agents:{status}"
        self.redis.sadd(status_key, agent_id)

        # Remove from other status sets
        for other_status in ["available", "busy", "after-call", "break", "offline"]:
            if other_status != status:
                self.redis.srem(f"agents:{other_status}", agent_id)

        # LIFE-04 fix (2026-06-02 audit): FIFO routing reads
        # ``agent_last_assigned:<agent_id>`` to pick the longest-idle agent,
        # but nothing in the codebase ever wrote that key — every FIFO pick
        # saw None and short-circuited to the first available agent
        # (alphabetical by id), collapsing FIFO to non-fair selection.
        # Whenever an agent transitions INTO 'busy', stamp the timestamp.
        # We bound the key TTL to the same 8h as the agent status itself
        # so abandoned keys don't accumulate.
        if status == 'busy':
            try:
                ts_key = f"agent_last_assigned:{agent_id}"
                self.redis.setex(ts_key, 28800, str(datetime.utcnow().timestamp()))
            except Exception as e:
                # Don't fail the status write — FIFO degrading to non-fair
                # selection is preferable to losing the busy mark.
                logger.warning(
                    f"set_agent_status: failed to stamp agent_last_assigned "
                    f"for {agent_id}: {e}"
                )

        logger.info(f"Agent {agent_id} status changed to {status}")

        # Push-dispatch on transition into 'available'. The caller is already
        # in their conference (placed there by /direct-inbound), so this just
        # notifies the agent — their frontend banner pops, on Accept their
        # leg joins the conference. Sub-second from go-available to ring.
        # Best-effort: failures here must NEVER break the status write above.
        if status == 'available' and previous_status != 'available':
            try:
                self._rehydrate_queue_activations(agent_id)
            except Exception as e:
                logger.warning(
                    f"Queue-activation rehydrate for agent {agent_id} failed: {e}"
                )
            try:
                self._push_dispatch_waiting_call(agent_id)
            except Exception as e:
                logger.warning(
                    f"Push-dispatch on agent {agent_id} available failed: {e}"
                )

    def _rehydrate_queue_activations(self, agent_id: str) -> None:
        """Re-assert this agent's ``queue_agents:{slug}`` memberships from
        the DB before dispatch consults them.

        Redis is the runtime routing source, but it loses state (restart,
        demo-reset FLUSHDB) while ``QueueAgentAssignment.is_activated``
        survives — the UI checkbox (DB-backed) then shows a queue ON that
        routing (Redis-backed) treats as OFF, and with no all-agents
        fallback the agent's calls hold forever. Healing on the available
        transition closes the skew at exactly the moment the agent starts
        expecting calls.
        """
        from app.models.queue import QueueAgentAssignment
        try:
            uid = int(agent_id)
        except (TypeError, ValueError):
            return
        assignments = (
            QueueAgentAssignment.query
            .filter_by(user_id=uid, is_activated=True)
            .all()
        )
        for assignment in assignments:
            queue = getattr(assignment, 'queue', None)
            if queue is None or not queue.slug:
                continue
            # Key by the QUEUE row's workspace — the assignment's queue and
            # agent always share one, but the row is the authority.
            self.redis.sadd(
                self._ws_agents_key(queue.slug, queue.workspace_id), str(uid)
            )

    # ---- decline cooldown ---------------------------------------------
    # When an agent declines an assignment, we re-queue the call and free
    # the agent. Without a cooldown, the freshly-available agent
    # immediately gets push-dispatched to the SAME call (they're the only
    # available agent in single-agent test scenarios), which retriggers
    # their banner → infinite decline loop the user reported. Track
    # recently-declined (agent_id, call_sid) pairs with a short TTL so
    # push-dispatch skips them.
    _DECLINE_COOLDOWN_SECONDS = 60

    def mark_decline(self, agent_id: str, call_sid: str) -> None:
        """Record that this agent just declined this call. Push-dispatch
        will skip them for this call until the cooldown expires."""
        if not (agent_id and call_sid):
            return
        try:
            self.redis.setex(
                f"decline_cooldown:{agent_id}:{call_sid}",
                self._DECLINE_COOLDOWN_SECONDS,
                '1',
            )
        except Exception as e:
            logger.warning(f"mark_decline({agent_id}, {call_sid}) failed: {e}")

    def has_recently_declined(self, agent_id: str, call_sid: str) -> bool:
        """Was (agent_id, call_sid) recently declined? Used by
        push-dispatch to skip recently-declined pairings."""
        if not (agent_id and call_sid):
            return False
        try:
            return bool(self.redis.exists(f"decline_cooldown:{agent_id}:{call_sid}"))
        except Exception:
            return False

    def _push_dispatch_waiting_call(self, agent_id: str) -> None:
        """Notify ``agent_id`` about the oldest waiting call in any queue
        they're activated for. Caller is already in a conference; agent's
        WebRTC leg joins it via the existing call-assignment banner flow.

        No-op if the agent isn't activated for any queue, no calls are
        waiting, or the agent doesn't have a Call Fabric subscriber address
        the system can dial via the AGENT_CONFERENCE_RESOURCE.
        """
        # Resolve the agent user FIRST — their workspace bounds everything
        # below (§8.3): the activation scan, the Queue/Call lookups and the
        # queue keys all stay inside ``ws:{agent's workspace}:``. Scanning
        # the bare ``queue_agents:*`` namespace would sweep every
        # workspace's activation sets and dispatch across tenants.
        try:
            from app.models import User
            from app.tenancy import DEFAULT_WORKSPACE_ID
        except Exception as e:
            logger.error(f"Push-dispatch: failed to import User: {e}")
            return
        try:
            agent_user = User.query.filter_by(id=int(agent_id)).first()
        except (ValueError, TypeError):
            agent_user = None
        if not agent_user:
            logger.warning(f"Push-dispatch: agent {agent_id} not in DB; skipping")
            return
        agent_ws_id = agent_user.workspace_id

        # Find queues this agent is activated for — within their workspace.
        activation_prefix = self._ws_agents_key('', agent_ws_id)
        activated_queues = []
        for raw_key in self.redis.scan_iter(f"{activation_prefix}*"):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            slug = key[len(activation_prefix):]
            if slug and self.redis.sismember(key, agent_id):
                activated_queues.append(slug)
        if not activated_queues:
            return

        # Branch on queue transport. Bridge-mode queues use SignalWire's
        # native enter_queue parking — the waiting callers are NOT in our
        # Redis zset; SignalWire owns them. Pickup is a server-side dial
        # to the agent that bridges them via `connect: queue:<slug>` SWML.
        # Conference-mode queues use the existing Redis-zset + call_assignment
        # pattern below.
        try:
            from app.models import Queue, Call
            bridge_dispatched = False
            for slug in activated_queues:
                # Slugs repeat across workspaces — pin lookups to the
                # agent's workspace (this runs with no request context, so
                # the auto-filter is off).
                q = Queue.query.filter_by(
                    slug=slug, is_active=True,
                    workspace_id=agent_ws_id or DEFAULT_WORKSPACE_ID,
                ).first()
                if not q or (q.routing_transport or 'conference') != 'bridge':
                    continue
                # Only dial if there's at least one waiting bridge-mode caller
                # (mirrored from enter_queue status_url callbacks into Call.status).
                # Without this gate we'd ring the agent every time they go
                # available even when the queue is empty.
                waiting = Call.query.filter_by(
                    queue_id=slug, status='waiting', transport='bridge',
                    workspace_id=agent_ws_id or DEFAULT_WORKSPACE_ID,
                ).count()
                if waiting <= 0:
                    continue
                if self._push_dispatch_bridge_pickup(agent_id, slug):
                    bridge_dispatched = True
                    break  # one pickup at a time
            if bridge_dispatched:
                return
        except Exception as e:
            logger.warning(
                f"Push-dispatch bridge-mode check failed (non-fatal): {e}"
            )

        for slug in activated_queues:
            queue_key = self._ws_queue_key(slug, agent_ws_id)
            head = self.redis.zrange(queue_key, 0, 0)
            if not head:
                continue
            raw = head[0]
            try:
                call_data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                continue
            call_sid = call_data.get('call_id')
            if not call_sid:
                continue

            # Skip if this agent recently declined this specific call —
            # prevents the infinite-banner-loop when an agent declines
            # and is then the only available candidate.
            if self.has_recently_declined(agent_id, call_sid):
                logger.info(
                    f"Push-dispatch: agent {agent_id} recently declined "
                    f"call {call_sid}; skipping (cooldown active)"
                )
                continue

            # Look up the Call ORM row. We can't import at module top —
            # circular dep with app.__init__. Defer imports here.
            try:
                from app import db
                from app.models import Call
                from app.services.queue_dispatch import emit_call_assignment_to_agent
                from datetime import datetime
            except Exception as e:
                logger.error(f"Push-dispatch: failed to import deps: {e}")
                return

            call = Call.find_by_sid(call_sid)
            if not call:
                logger.warning(f"Push-dispatch: call {call_sid} not in DB; skipping")
                continue
            # Belt-and-braces: the zset was already read from the agent's
            # workspace prefix, but never bridge a call whose row says it
            # belongs elsewhere (stale/quarantined entries).
            if (call.workspace_id or DEFAULT_WORKSPACE_ID) != (
                agent_ws_id or DEFAULT_WORKSPACE_ID
            ):
                logger.warning(
                    f"Push-dispatch: call {call_sid} workspace "
                    f"{call.workspace_id} != agent workspace {agent_ws_id}; skipping"
                )
                continue

            # Language policy, from the other direction. Immediate dispatch
            # asks "may I give this call to a mismatched agent?"; here an
            # agent has just gone available and we ask the same question about
            # the call at the head of the queue. Without this, wait_only and
            # wait_then_translate held out at arrival and then handed the call
            # to the first mismatched agent who came free seconds later —
            # policy honoured on one path and ignored on the other, which is
            # the drift this codebase keeps producing.
            try:
                from app.models import Queue as QueueModel
                from app.services.call_language import (
                    language_fallback_allowed, derive_call_language,
                )
                caller_language = derive_call_language(call)
                spoken = agent_user.languages or []
                if caller_language and spoken and caller_language not in spoken:
                    queue_row = QueueModel.query.filter_by(
                        slug=slug,
                        workspace_id=call.workspace_id or DEFAULT_WORKSPACE_ID,
                    ).first()
                    waited = None
                    enqueued_at = call_data.get('originally_received_at') or                         call_data.get('enqueued_at')
                    if enqueued_at:
                        try:
                            waited = (
                                datetime.utcnow()
                                - datetime.fromisoformat(str(enqueued_at))
                            ).total_seconds()
                        except Exception:
                            waited = None
                    if not language_fallback_allowed(queue_row, waited_seconds=waited):
                        logger.info(
                            f"Push-dispatch: agent {agent_id} does not speak "
                            f"{caller_language} and queue '{slug}' policy says "
                            "keep waiting — leaving call %s queued", call_sid,
                        )
                        continue
            except Exception as e:
                # Never let the policy check cost a caller their dispatch.
                logger.warning(
                    f"Push-dispatch: language policy check failed on {call_sid} "
                    f"(dispatching anyway): {e}"
                )

            if not agent_user.signalwire_address:
                logger.warning(
                    f"Push-dispatch: agent {agent_id} has no signalwire_address; "
                    f"cannot dial. Skipping."
                )
                return

            # Conference name is deterministic — set by /direct-inbound.
            conference_name = call.conference_name or f"interaction-{call_sid}"

            # Atomic claim — race-safe assignment. Two parallel push-dispatch
            # instances (e.g. two agents going Available simultaneously) used
            # to both read the same waiting call, both write
            # `assigned_agent_id`, and both fire `call_assignment` banners.
            # Last writer won on the DB row but the loser's agent still saw
            # the banner — clicking Take then failed with "assigned to
            # another agent" because the row pointed elsewhere by then.
            #
            # ``UPDATE calls SET assigned_agent_id=... WHERE id=... AND
            # assigned_agent_id IS NULL`` is atomic at the Postgres level:
            # only one parallel claim succeeds, the other gets 0 rows and
            # bails before the banner fires. No more phantom assignments.
            try:
                from sqlalchemy import text
                claim_at = datetime.utcnow()
                claim = db.session.execute(
                    text(
                        "UPDATE calls "
                        "SET assigned_agent_id = :uid, assigned_at = :ts, status = 'assigned' "
                        "WHERE id = :id AND assigned_agent_id IS NULL "
                        "RETURNING id"
                    ),
                    {
                        'uid': agent_user.id,
                        'ts': claim_at,
                        'id': call.id,
                    },
                )
                if claim.fetchone():
                    # Same language-fallback wire as immediate dispatch. A
                    # caller who waited is MORE likely to need it, not less:
                    # waiting is what happens when nobody who speaks their
                    # language was free.
                    try:
                        from app.services.call_language import (
                            flag_translation_if_mismatched,
                        )
                        flag_translation_if_mismatched(call, agent_user)
                    except Exception as e:
                        logger.warning(
                            f"Push-dispatch: translation flag failed on call "
                            f"{call_sid} (non-fatal): {e}"
                        )
                else:
                    # Lost the race — another worker already claimed this
                    # call. Don't emit, don't mark agent busy. The agent
                    # stays available for the next call.
                    db.session.rollback()
                    logger.info(
                        f"Push-dispatch: lost race on call {call_sid} — "
                        f"another worker claimed it before us. Agent "
                        f"{agent_id} stays available."
                    )
                    return
                from app.services.interaction_timeline import best_effort, record_queue_offered
                best_effort(record_queue_offered, call, agent_user.id, claim_at)
                db.session.commit()
                # Refresh the in-memory call object so downstream emit reads
                # the latest assigned_agent_id / status fields.
                db.session.refresh(call)
            except Exception as e:
                logger.error(f"Push-dispatch: DB update failed for call {call_sid}: {e}")
                db.session.rollback()
                return

            # Mark agent busy on this specific call so other transitions don't
            # also try to dispatch them.
            try:
                self.set_agent_status(agent_id, 'busy', current_call_id=call_sid)
            except Exception as e:
                logger.warning(f"Push-dispatch: set_agent_status to busy failed: {e}")

            # Remove from queue zset — they're no longer waiting.
            try:
                self.remove_call_from_all_queues(call_sid)
            except Exception as e:
                logger.warning(f"Push-dispatch: remove from queue failed: {e}")

            # Parse context for the banner (best-effort)
            context = {}
            try:
                if call.ai_context:
                    context = json.loads(call.ai_context)
            except Exception:
                context = {}

            emit_call_assignment_to_agent(
                call=call,
                agent=agent_user,
                conference_name=conference_name,
                queue_slug=slug,
                context=context,
            )
            logger.info(
                f"Push-dispatched call {call_sid} (queue={slug}) → "
                f"agent {agent_id} via conference {conference_name}"
            )
            return  # one dispatch per available transition

    def _push_dispatch_bridge_pickup(self, agent_id: str, queue_slug: str) -> bool:
        """Dispatch a bridge-mode pickup: server-side dial to the agent's
        Fabric address, with the queue-pickup SWML as the dial source. When
        the agent answers, SWML executes ``connect: queue:<slug>`` which
        pops the next parked caller and bridges. No conference.

        Returns True if the dial was issued, False on any precondition fail
        (no User row, no signalwire_address, dial failure). Caller decides
        whether to fall back to conference-mode dispatch on False.
        """
        try:
            from app.models import User
            from app.services.signalwire_api import get_signalwire_api
            from app.utils.url_utils import get_base_url, signed_webhook_url
        except Exception as e:
            logger.error(f"Bridge pickup: import failed: {e}")
            return False

        try:
            agent_user = User.query.filter_by(id=int(agent_id)).first()
        except (ValueError, TypeError):
            agent_user = None
        if not agent_user:
            logger.warning(f"Bridge pickup: agent {agent_id} not in DB")
            return False
        if not agent_user.signalwire_address:
            logger.warning(
                f"Bridge pickup: agent {agent_id} has no signalwire_address"
            )
            return False

        # Mark busy BEFORE issuing the dial so a parallel transition doesn't
        # double-dispatch the same agent. The current_call_id is blank — we
        # won't know the popped caller's call_id until the connect resolves.
        try:
            self.set_agent_status(agent_id, 'busy', current_call_id=None)
        except Exception as e:
            logger.warning(f"Bridge pickup: set_agent_status to busy failed: {e}")

        base_url = get_base_url()
        swml_url = signed_webhook_url(
            f"{base_url}/api/swml/queue-pickup/{queue_slug}"
        )
        status_callback = signed_webhook_url(
            f"{base_url}/api/webhooks/call-status"
        )
        try:
            sw_api = get_signalwire_api()
            result = sw_api.dial_to_queue_pickup(
                to_address=agent_user.signalwire_address,
                queue_slug=queue_slug,
                swml_url=swml_url,
                status_callback=status_callback,
            )
            logger.info(
                f"Bridge pickup dispatched: agent={agent_id} "
                f"queue={queue_slug} dial_sid={getattr(result, 'sid', None)}"
            )
            return True
        except Exception as e:
            logger.error(
                f"Bridge pickup dial failed for agent {agent_id} queue {queue_slug}: {e}"
            )
            # CRITICAL: do NOT call self.set_agent_status('available') here.
            # That helper triggers _push_dispatch_waiting_call on every
            # transition into 'available', which re-enters this code path
            # — infinite recursion until Python's stack limit (we hit
            # "maximum recursion depth exceeded" on agent 1 in prod).
            # Revert agent state directly via Redis so the flip doesn't
            # re-fire the dispatch hook. The agent is now "available with
            # a stuck dispatch" — they can try going offline+available
            # again to force a fresh attempt.
            try:
                agent_key = f"{self.agent_prefix}{agent_id}"
                self.redis.set(agent_key, json.dumps({
                    'agent_id': str(agent_id),
                    'status': 'available',
                    'current_call_id': None,
                    'last_status_change': datetime.utcnow().isoformat(),
                }))
            except Exception:
                pass
            return False

    def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get current agent status"""
        agent_key = f"{self.agent_prefix}{agent_id}"
        try:
            data = self.redis.get(agent_key)
            if data:
                return json.loads(data)
        except redis.exceptions.ResponseError:
            # Key exists but is wrong type (HASH from legacy code) — delete and return None
            self.redis.delete(agent_key)
            logger.warning(f"Deleted stale agent key with wrong type: {agent_key}")
        return None

    def get_available_agents(self, queue_slug: Optional[str] = None) -> List[str]:
        """
        Get list of available agents, optionally filtered by queue activation.

        Args:
            queue_slug: Optional queue slug to filter by activated agents

        Returns:
            Sorted list of available agent ID strings
        """
        available = self.redis.smembers("agents:available")

        if not queue_slug:
            return sorted(list(available))

        # Only agents who ACTIVATED this queue are candidates. No fallback
        # to the full available pool: that silently overrode the queue-
        # activation contract (agents got calls for queues they never
        # opted into), and under workspace tenancy it becomes a hard
        # cross-tenant mis-route. No activated agents -> empty list; the
        # caller's no-agent path (hold / AI fallback) handles it honestly.
        # The activation set is workspace-prefixed, so even though
        # ``agents:available`` is a global set of user ids, the
        # intersection can only ever contain this workspace's agents.
        queue_agents = self.redis.smembers(self._ws_agents_key(queue_slug))
        filtered = sorted(available & queue_agents)
        if not filtered and available:
            logger.info(
                "get_available_agents: %d agent(s) available but none activated "
                "queue '%s' — returning no candidates", len(available), queue_slug,
            )
        return filtered

    def select_agent(self, queue_slug: str, routing_strategy: str,
                     available_agents: List[str], skill_levels: Dict[str, int] = None,
                     call_priority: int = 5,
                     caller_language: Optional[str] = None,
                     agent_languages: Optional[Dict[str, List[str]]] = None,
                     allow_language_fallback: bool = True) -> Optional[str]:
        """Select the next agent based on the queue's routing strategy.

        Language preference runs *before* the strategy: agents whose `languages`
        list includes the caller's language are preferred. If none match, we fall
        back to the full available pool — translation will then start at conference
        join (caller is responsible for setting `needs_translation` on the Call).

        Args:
            queue_slug: Queue identifier for round-robin state tracking
            routing_strategy: 'fifo', 'round_robin', 'priority', 'skill_based'
            available_agents: Sorted list of available agent ID strings
            skill_levels: Dict mapping agent_id -> skill_level (for skill_based)
            call_priority: 1-10 priority of the incoming call (for priority routing)
            caller_language: BCP-47 code (e.g. 'es-ES') the caller speaks
            agent_languages: Map of agent_id -> list of BCP-47 codes they speak

        Returns:
            Selected agent ID string, or None if no agents available
        """
        if not available_agents:
            return None

        # Language-preference pass: narrow to agents who speak the caller's language
        candidate_pool = available_agents
        if caller_language and agent_languages:
            matching = [
                a for a in available_agents
                if caller_language in (agent_languages.get(a) or [])
            ]
            if not matching and not allow_language_fallback:
                # The queue's policy says hold out for a real speaker rather
                # than settle for translation right now. Returning nobody
                # leaves the caller in the queue, where the hold cycle and the
                # existing hold cap still apply — so "wait" never means
                # "wait forever", it means "wait until the cap turns this into
                # a callback".
                logger.info(
                    f"No agents speak {caller_language} in queue "
                    f"'{queue_slug}' and the policy declines to fall back — "
                    "leaving the caller queued"
                )
                return None
            if matching:
                candidate_pool = matching
                logger.info(
                    f"Language match: {len(matching)}/{len(available_agents)} agents speak "
                    f"{caller_language} for queue '{queue_slug}'"
                )
            else:
                logger.info(
                    f"No agents speak {caller_language} in queue '{queue_slug}' — "
                    f"falling back to all available, translation will be needed"
                )

        if routing_strategy == 'fifo':
            return self._strategy_fifo(candidate_pool)
        elif routing_strategy == 'round_robin':
            return self._strategy_round_robin(queue_slug, candidate_pool)
        elif routing_strategy == 'priority':
            return self._strategy_priority(candidate_pool, skill_levels or {}, call_priority)
        elif routing_strategy == 'skill_based':
            return self._strategy_skill_based(candidate_pool, skill_levels or {})
        else:
            return self._strategy_round_robin(queue_slug, candidate_pool)

    def _strategy_fifo(self, available_agents: List[str]) -> str:
        """FIFO: pick the agent who has been idle longest (oldest last_assigned timestamp)."""
        oldest_time = None
        oldest_agent = available_agents[0]

        for agent_id in available_agents:
            last_assigned = self.redis.get(f"agent_last_assigned:{agent_id}")
            if last_assigned is None:
                return agent_id  # Never assigned = longest idle
            ts = float(last_assigned)
            if oldest_time is None or ts < oldest_time:
                oldest_time = ts
                oldest_agent = agent_id

        return oldest_agent

    def _strategy_round_robin(self, queue_slug: str, available_agents: List[str]) -> str:
        """Round-robin: cycle through agents in order."""
        rr_key = self._ws_rr_key(queue_slug)
        last_index_raw = self.redis.get(rr_key)
        last_index = int(last_index_raw) if last_index_raw else -1
        next_index = (last_index + 1) % len(available_agents)
        self.redis.set(rr_key, next_index)
        return available_agents[next_index]

    def _strategy_priority(self, available_agents: List[str],
                           skill_levels: Dict[str, int], call_priority: int) -> str:
        """Priority-based: high-priority calls get the most skilled agent,
        low-priority calls get the least skilled (preserve experts for urgent work)."""
        agents_with_skill = [
            (agent_id, skill_levels.get(agent_id, 5))
            for agent_id in available_agents
        ]

        if call_priority <= 3:  # High priority
            agents_with_skill.sort(key=lambda x: x[1], reverse=True)
        else:
            agents_with_skill.sort(key=lambda x: x[1])

        return agents_with_skill[0][0]

    def _strategy_skill_based(self, available_agents: List[str],
                              skill_levels: Dict[str, int]) -> str:
        """Skill-based: always pick the agent with the highest skill level."""
        best_agent = available_agents[0]
        best_skill = skill_levels.get(best_agent, 0)

        for agent_id in available_agents[1:]:
            skill = skill_levels.get(agent_id, 0)
            if skill > best_skill:
                best_skill = skill
                best_agent = agent_id

        return best_agent

    def get_skill_levels_for_queue(self, queue_slug: str, agent_ids: List[str]) -> Dict[str, int]:
        """Get skill levels for agents in a specific queue from database."""
        from app.models.queue import QueueAgentAssignment, Queue
        from app.tenancy import DEFAULT_WORKSPACE_ID
        # Slugs repeat across workspaces and webhook callers have no
        # request scope — pin to this service's workspace.
        queue = Queue.query.filter_by(
            slug=queue_slug,
            workspace_id=self.workspace_id or DEFAULT_WORKSPACE_ID,
        ).first()
        if not queue:
            return {}

        numeric_ids = [int(a) for a in agent_ids if a.isdigit()]
        if not numeric_ids:
            return {}

        assignments = QueueAgentAssignment.query.filter(
            QueueAgentAssignment.queue_id == queue.id,
            QueueAgentAssignment.user_id.in_(numeric_ids)
        ).all()

        return {str(a.user_id): a.skill_level for a in assignments}

    def get_languages_for_agents(self, agent_ids: List[str]) -> Dict[str, List[str]]:
        """Look up the BCP-47 languages each agent speaks, from User.languages."""
        from app.models import User

        numeric_ids = [int(a) for a in agent_ids if a.isdigit()]
        if not numeric_ids:
            return {}

        users = User.query.filter(User.id.in_(numeric_ids)).all()
        return {str(u.id): (u.languages or ['en-US']) for u in users}

    def get_agents_by_status(self, status: str) -> List[str]:
        """Get all agents with a specific status"""
        status_key = f"agents:{status}"
        return list(self.redis.smembers(status_key))

    def transfer_call(
        self,
        call_id: str,
        from_agent_id: str,
        to_target: str,
        transfer_type: str = "blind"
    ) -> Dict[str, Any]:
        """
        Transfer a call to another agent or queue

        Args:
            call_id: Call to transfer
            from_agent_id: Agent initiating transfer
            to_target: Target agent ID or queue ID
            transfer_type: "blind" or "warm"

        Returns:
            Transfer result
        """
        # Get call data
        call_key = f"{self.call_prefix}{call_id}"
        call_data = self.redis.get(call_key)

        if not call_data:
            return {"success": False, "error": "Call not found"}

        call_info = json.loads(call_data)

        # Update transfer history
        transfer_log = call_info.get("transfer_history", [])
        transfer_log.append({
            "from": from_agent_id,
            "to": to_target,
            "type": transfer_type,
            "timestamp": datetime.utcnow().isoformat()
        })
        call_info["transfer_history"] = transfer_log

        # Handle transfer based on target type
        if to_target.startswith("queue-"):
            # Transfer to queue
            queue_id = to_target.replace("queue-", "")
            result = self.enqueue_call(
                call_id,
                queue_id,
                priority=7,  # Higher priority for transfers
                context=call_info.get("context"),
                caller_info=call_info.get("caller_info")
            )
            transfer_result = {"success": True, "target_type": "queue", "queue_info": result}
        else:
            # Transfer to specific agent
            target_status = self.get_agent_status(to_target)

            if not target_status or target_status["status"] != "available":
                return {"success": False, "error": "Target agent not available"}

            # Update agent statuses
            self.set_agent_status(from_agent_id, "available")
            self.set_agent_status(to_target, "busy", call_id)

            transfer_result = {"success": True, "target_type": "agent", "target_agent": to_target}

        # Update call data
        self.redis.setex(call_key, 3600, json.dumps(call_info))

        logger.info(f"Call {call_id} transferred from {from_agent_id} to {to_target}")

        return transfer_result

    def _get_queue_position(self, queue_id: str, call_id: str) -> int:
        """Get position of call in queue"""
        queue_key = self._ws_queue_key(queue_id)
        calls = self.redis.zrange(queue_key, 0, -1)

        for i, call_str in enumerate(calls):
            call_data = json.loads(call_str)
            if call_data["call_id"] == call_id:
                return i + 1

        return 0

    def _estimate_wait_time(self, queue_id: str, position: int) -> int:
        """Estimate wait time based on queue position and historical data"""
        # Simple estimation: 3 minutes per position
        # In production, use historical average handle time
        avg_handle_time = 180  # 3 minutes average

        return position * avg_handle_time

    def _remove_call_from_set(self, queue_key: str, call_id: str) -> int:
        """Remove all entries for a call_id from a specific queue sorted set."""
        removed = 0
        entries = self.redis.zrange(queue_key, 0, -1)
        for entry in entries:
            try:
                data = json.loads(entry)
                if data.get("call_id") == call_id:
                    self.redis.zrem(queue_key, entry)
                    removed += 1
            except (json.JSONDecodeError, TypeError) as exc:
                # Skip malformed entry — should never happen unless something
                # else wrote raw strings to the queue. Worth knowing about.
                logger.warning("queue entry not parseable as JSON, skipping: %s", exc)
        return removed

    def remove_call_from_all_queues(self, call_id: str) -> int:
        """Remove a call from all queues. Called when a call ends (hangup, timeout, etc.).

        Deliberately scans EVERY workspace's queue keys (``ws:*:queue:*``):
        call sids are globally unique, so this can only ever remove this
        call's own entries, and end-of-call cleanup paths (webhooks,
        watchdog) shouldn't strand a zset entry just because the row was
        re-parented between enqueue and hangup.
        """
        removed = 0
        # Scan all queue keys across workspaces
        for key in self.redis.scan_iter(f"ws:*:{self.queue_prefix}*"):
            removed += self._remove_call_from_set(key, call_id)
        # Also clean up the call data key
        self.redis.delete(f"{self.call_prefix}{call_id}")
        if removed:
            logger.info(f"Removed call {call_id} from {removed} queue entries")
        return removed

    def get_agent_metrics(self, agent_id: str, period_hours: int = 24) -> Dict[str, Any]:
        """Get performance metrics for an agent over a window.

        Handling segments are authoritative for new interactions. Calls that
        predate the timeline migration retain a non-overlapping legacy
        fallback, so the dashboard stays useful during rollout.
        """
        from app.services.interaction_timeline import get_agent_performance

        since = datetime.utcnow() - timedelta(hours=period_hours)
        try:
            agent_id_int = int(agent_id)
        except (TypeError, ValueError):
            agent_id_int = None

        performance = get_agent_performance(since, self.workspace_id)
        row = performance.get(agent_id_int, {}) if agent_id_int is not None else {}

        return {
            "agent_id": agent_id,
            "period_hours": period_hours,
            "calls_handled": row.get('calls_handled', 0),
            "average_handle_time": row.get('average_handle_time', 0.0),
            "current_status": self.get_agent_status(agent_id),
        }

    def get_queue_metrics(self, queue_id: str) -> Dict[str, Any]:
        """Get performance metrics for a queue"""
        status = self.get_queue_status(queue_id)

        # Honor the queue's configured SLA threshold (Settings → Queues).
        # Ad-hoc slugs without a Queue row fall back to the 60s default.
        sla_threshold = 60
        try:
            from app.models.queue import Queue
            queue_row = Queue.find_by_slug(queue_id)
            if queue_row and queue_row.sla_threshold_seconds:
                sla_threshold = queue_row.sla_threshold_seconds
        except Exception:
            pass

        return {
            **status,
            "available_agents": len(self.get_available_agents(queue_id)),
            "busy_agents": len([
                a for a in self.get_agents_by_status("busy")
                # Note: not yet filtered by per-agent queue assignment.
            ]),
            "service_level": self._calculate_service_level(
                queue_id, threshold_seconds=sla_threshold,
            ),
            "sla_threshold_seconds": sla_threshold,
        }

    def _calculate_service_level(
        self,
        queue_id: str,
        threshold_seconds: int = 60,
        window_hours: int = 24,
    ) -> Optional[float]:
        """Percentage of queue calls answered within ``threshold_seconds``.

        Computed over the past ``window_hours``. Returns ``None`` when
        there's no data in the window — callers should treat that as
        "not enough data" rather than coerce to 0.
        """
        since = datetime.utcnow() - timedelta(hours=window_hours)
        from app.services.interaction_timeline import calculate_service_level
        return calculate_service_level(
            queue_id,
            since,
            threshold_seconds,
            workspace_id=self.workspace_id,
        )
