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
    """Service for managing call queues and agent availability"""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.queue_prefix = "queue:"
        self.agent_prefix = "agent:"
        self.call_prefix = "call:"

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
        queue_key = f"{self.queue_prefix}{queue_id}"

        # Remove any existing entry for this call (prevents duplicates from hold loop retries)
        self._remove_call_from_set(queue_key, call_id)

        # Create call data
        call_data = {
            "call_id": call_id,
            "queue_id": queue_id,
            "priority": priority,
            "context": context or {},
            "caller_info": caller_info or {},
            "enqueued_at": datetime.utcnow().isoformat()
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

        # Notify available agents
        self._notify_agents(queue_id, call_id)

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
        queue_key = f"{self.queue_prefix}{queue_id}"

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
        queue_key = f"{self.queue_prefix}{queue_id}"

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
        queue_key = f"{self.queue_prefix}{queue_id}"
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

        logger.info(f"Agent {agent_id} status changed to {status}")

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

        # Filter by agents who have activated this queue
        queue_agents = self.redis.smembers(f"queue_agents:{queue_slug}")
        if queue_agents:
            filtered = list(available & queue_agents)
            if filtered:
                return sorted(filtered)
            # If intersection is empty (agents available but none activated this queue),
            # fall through to return all available agents as fallback

        # Fallback: return all available if no queue-specific filtering matched
        return sorted(list(available))

    def select_agent(self, queue_slug: str, routing_strategy: str,
                     available_agents: List[str], skill_levels: Dict[str, int] = None,
                     call_priority: int = 5) -> Optional[str]:
        """Select the next agent based on the queue's routing strategy.

        Args:
            queue_slug: Queue identifier for round-robin state tracking
            routing_strategy: 'fifo', 'round_robin', 'priority', 'skill_based'
            available_agents: Sorted list of available agent ID strings
            skill_levels: Dict mapping agent_id -> skill_level (for skill_based)
            call_priority: 1-10 priority of the incoming call (for priority routing)

        Returns:
            Selected agent ID string, or None if no agents available
        """
        if not available_agents:
            return None

        if routing_strategy == 'fifo':
            return self._strategy_fifo(available_agents)
        elif routing_strategy == 'round_robin':
            return self._strategy_round_robin(queue_slug, available_agents)
        elif routing_strategy == 'priority':
            return self._strategy_priority(available_agents, skill_levels or {}, call_priority)
        elif routing_strategy == 'skill_based':
            return self._strategy_skill_based(available_agents, skill_levels or {})
        else:
            return self._strategy_round_robin(queue_slug, available_agents)

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
        rr_key = f"round_robin:{queue_slug}"
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
        queue = Queue.find_by_slug(queue_slug)
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
        queue_key = f"{self.queue_prefix}{queue_id}"
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
            except (json.JSONDecodeError, TypeError):
                pass
        return removed

    def remove_call_from_all_queues(self, call_id: str) -> int:
        """Remove a call from all queues. Called when a call ends (hangup, timeout, etc.)."""
        removed = 0
        # Scan all queue keys
        for key in self.redis.scan_iter(f"{self.queue_prefix}*"):
            removed += self._remove_call_from_set(key, call_id)
        # Also clean up the call data key
        self.redis.delete(f"{self.call_prefix}{call_id}")
        if removed:
            logger.info(f"Removed call {call_id} from {removed} queue entries")
        return removed

    def _notify_agents(self, queue_id: str, call_id: str) -> None:
        """Notify available agents about new call in queue"""
        available_agents = self.get_available_agents(queue_id)

        if available_agents:
            # Publish notification via Redis pub/sub
            notification = {
                "type": "new_call_in_queue",
                "queue_id": queue_id,
                "call_id": call_id,
                "timestamp": datetime.utcnow().isoformat()
            }

            channel = f"queue_notifications:{queue_id}"
            self.redis.publish(channel, json.dumps(notification))

            logger.info(f"Notified {len(available_agents)} agents about call {call_id}")

    def get_agent_metrics(self, agent_id: str, period_hours: int = 24) -> Dict[str, Any]:
        """
        Get performance metrics for an agent

        Args:
            agent_id: Agent identifier
            period_hours: Time period to calculate metrics

        Returns:
            Agent performance metrics
        """
        # In production, query from database
        # This is a simplified version using Redis data

        return {
            "agent_id": agent_id,
            "period_hours": period_hours,
            "calls_handled": 0,  # Would query from database
            "average_handle_time": 0,
            "average_wait_time": 0,
            "transfer_rate": 0,
            "current_status": self.get_agent_status(agent_id)
        }

    def get_queue_metrics(self, queue_id: str) -> Dict[str, Any]:
        """Get performance metrics for a queue"""
        status = self.get_queue_status(queue_id)

        return {
            **status,
            "available_agents": len(self.get_available_agents(queue_id)),
            "busy_agents": len([
                a for a in self.get_agents_by_status("busy")
                # Filter by queue assignment in production
            ]),
            "service_level": self._calculate_service_level(queue_id)
        }

    def _calculate_service_level(self, queue_id: str, threshold_seconds: int = 60) -> float:
        """Calculate percentage of calls answered within threshold"""
        # In production, query historical data from database
        # This is a placeholder
        return 85.0  # 85% of calls answered within threshold