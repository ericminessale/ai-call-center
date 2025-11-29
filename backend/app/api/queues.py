"""
Queue Management API Endpoints
Handles call queuing, agent assignment, and queue monitoring
"""

from flask import Blueprint, jsonify, request, current_app
from app.services.queue_service import QueueService
from app.services.redis_service import get_redis_client
from app.utils.decorators import require_auth
from app import db
from app.models import Call, User
from datetime import datetime
import logging
import json

logger = logging.getLogger(__name__)

queues_bp = Blueprint('queues', __name__)

# Initialize queue service
queue_service = None


def get_queue_service():
    """Get or create queue service instance"""
    global queue_service
    if queue_service is None:
        redis_client = get_redis_client()
        queue_service = QueueService(redis_client)
    return queue_service


@queues_bp.route('/<queue_id>/route', methods=['POST'])
def route_call_to_queue(queue_id):
    """
    Route an incoming call to a queue
    Called by AI agents via SWML transfer
    """
    try:
        data = request.json or {}

        # Extract call information from SignalWire webhook
        call_id = data.get('CallSid') or data.get('call_id')
        caller_number = data.get('From') or data.get('caller_number')

        # Get context from AI agent (passed via headers or body)
        context = {
            'customer_name': data.get('customer_name'),
            'account_number': data.get('account_number'),
            'issue_description': data.get('issue_description'),
            'priority': data.get('priority', 5),
            'ai_summary': data.get('ai_summary'),
            'global_data': data.get('global_data', {})
        }

        # Clean up None values
        context = {k: v for k, v in context.items() if v is not None}

        # Get priority from context or default
        priority = context.get('priority', 5)

        # Create or update call record in database
        call = Call.query.filter_by(call_sid=call_id).first()
        if not call:
            call = Call(
                call_sid=call_id,
                from_number=caller_number,
                to_number=data.get('To'),
                status='queued',
                direction='inbound',
                created_at=datetime.utcnow()
            )
            db.session.add(call)

        call.customer_context = context
        call.queue_id = queue_id
        db.session.commit()

        # Enqueue the call
        service = get_queue_service()
        queue_result = service.enqueue_call(
            call_id=call_id,
            queue_id=queue_id,
            priority=priority,
            context=context,
            caller_info={
                'number': caller_number,
                'name': context.get('customer_name')
            }
        )

        # Check for available agents
        available_agents = service.get_available_agents(queue_id)

        if available_agents:
            # Immediately route to available agent
            agent_id = available_agents[0]

            # Get agent's SIP address or phone number (simplified for now)
            user = User.query.filter_by(id=agent_id).first()
            if user:
                # Use email as SIP address for demo purposes
                transfer_target = f"sip:{user.email}@signalwire.local"

                # Dequeue the call for this agent
                call_data = service.dequeue_call(queue_id, agent_id)

                logger.info(f"Routing call {call_id} to available agent {agent_id}")

                # Return SWML response to transfer the call
                return jsonify({
                    "sections": {
                        "main": [{
                            "play": {
                                "url": "say:Connecting you to the next available specialist."
                            }
                        }, {
                            "connect": {
                                "to": transfer_target,
                                "headers": {
                                    "X-Customer-Context": str(context),
                                    "X-Queue-Wait-Time": str(call_data.get('wait_time_seconds', 0))
                                }
                            }
                        }]
                    }
                })

        # No agents available - place in queue with hold music
        logger.info(f"Call {call_id} queued at position {queue_result['position']}")

        return jsonify({
            "sections": {
                "main": [{
                    "play": {
                        "url": f"say:All of our specialists are currently helping other customers. "
                               f"You are number {queue_result['position']} in the queue. "
                               f"Your estimated wait time is {queue_result['estimated_wait_seconds'] // 60} minutes."
                    }
                }, {
                    "play": {
                        "url": "https://cdn.signalwire.com/swml/hold_music.mp3",
                        "loop": True
                    }
                }]
            }
        })

    except Exception as e:
        logger.error(f"Error routing call to queue {queue_id}: {str(e)}")
        return jsonify({
            "sections": {
                "main": [{
                    "play": {
                        "url": "say:We're experiencing technical difficulties. Please try again later."
                    }
                }, {
                    "hangup": {}
                }]
            }
        }), 500


@queues_bp.route('/<queue_id>/next', methods=['GET'])
@require_auth
def get_next_queued_call(queue_id):
    """
    Agent requests the next call from their queue
    """
    try:
        # Get agent ID from authenticated user
        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()

        # Set agent as available if not already
        service.set_agent_status(agent_id, "available")

        # Dequeue next call
        call_data = service.dequeue_call(queue_id, agent_id)

        if not call_data:
            return jsonify({"message": "No calls in queue"}), 204

        # Update call record
        call = Call.query.filter_by(call_sid=call_data['call_id']).first()
        if call:
            call.status = 'in-progress'
            db.session.commit()

        logger.info(f"Agent {agent_id} took call {call_data['call_id']} from queue {queue_id}")

        return jsonify(call_data)

    except Exception as e:
        logger.error(f"Error getting next call from queue: {str(e)}")
        return jsonify({"error": "Failed to get next call"}), 500


@queues_bp.route('/<queue_id>/status', methods=['GET'])
@require_auth
def get_queue_status(queue_id):
    """
    Get current queue statistics
    """
    try:
        service = get_queue_service()
        status = service.get_queue_status(queue_id)
        metrics = service.get_queue_metrics(queue_id)

        return jsonify({
            **status,
            **metrics
        })

    except Exception as e:
        logger.error(f"Error getting queue status: {str(e)}")
        return jsonify({"error": "Failed to get queue status"}), 500


@queues_bp.route('/agent/status', methods=['PUT'])
@require_auth
def update_agent_status():
    """
    Update agent's availability status
    """
    try:
        data = request.json
        new_status = data.get('status')

        if new_status not in ['available', 'busy', 'break', 'offline']:
            return jsonify({"error": "Invalid status"}), 400

        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()
        current_call_id = data.get('current_call_id')

        service.set_agent_status(agent_id, new_status, current_call_id)

        # If going available, check for queued calls
        next_call = None
        if new_status == 'available':
            # Check all configured queues
            for queue_id in ['sales', 'support', 'billing']:
                call_data = service.dequeue_call(queue_id, agent_id)
                if call_data:
                    next_call = call_data
                    break

        logger.info(f"Agent {agent_id} status changed to {new_status}")

        return jsonify({
            "status": new_status,
            "next_call": next_call
        })

    except Exception as e:
        logger.error(f"Error updating agent status: {str(e)}")
        return jsonify({"error": "Failed to update status"}), 500


@queues_bp.route('/agent/metrics', methods=['GET'])
@require_auth
def get_agent_metrics():
    """
    Get performance metrics for the current agent
    """
    try:
        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        period_hours = request.args.get('period_hours', 24, type=int)

        service = get_queue_service()
        metrics = service.get_agent_metrics(agent_id, period_hours)

        # Add database metrics
        from sqlalchemy import func
        from datetime import timedelta

        since = datetime.utcnow() - timedelta(hours=period_hours)

        # For now, return mock metrics since we don't have agent_id on calls
        calls_handled = 15
        avg_duration = 240

        metrics.update({
            'calls_handled': calls_handled,
            'average_handle_time': avg_duration
        })

        return jsonify(metrics)

    except Exception as e:
        logger.error(f"Error getting agent metrics: {str(e)}")
        return jsonify({"error": "Failed to get metrics"}), 500


@queues_bp.route('/transfer', methods=['POST'])
@require_auth
def transfer_call():
    """
    Transfer a call to another agent or queue
    """
    try:
        data = request.json
        call_id = data.get('call_id')
        target = data.get('target')  # agent_id or queue_id
        transfer_type = data.get('type', 'blind')  # blind or warm

        if not call_id or not target:
            return jsonify({"error": "Missing required fields"}), 400

        agent_id = request.current_user.id
        if not agent_id:
            return jsonify({"error": "User not authenticated"}), 403

        service = get_queue_service()
        result = service.transfer_call(call_id, agent_id, target, transfer_type)

        if not result['success']:
            return jsonify(result), 400

        # Update call record
        call = Call.query.filter_by(call_sid=call_id).first()
        if call:
            # Store transfer history as JSON string
            import json
            transfer_history = json.loads(call.transfer_history or '[]')
            transfer_history.append({
                'from': agent_id,
                'to': target,
                'type': transfer_type,
                'timestamp': datetime.utcnow().isoformat()
            })
            call.transfer_history = json.dumps(transfer_history)
            db.session.commit()

        logger.info(f"Call {call_id} transferred from {agent_id} to {target}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"Error transferring call: {str(e)}")
        return jsonify({"error": "Failed to transfer call"}), 500


@queues_bp.route('/all/status', methods=['GET'])
@require_auth
def get_all_queues_status():
    """
    Get status of all queues
    """
    try:
        redis_client = get_redis_client()
        if not redis_client:
            return jsonify({"error": "Redis not available"}), 503

        # Define available queues
        queue_ids = ['sales', 'support', 'billing']

        all_status = []
        for queue_id in queue_ids:
            queue_key = f"queue:{queue_id}"
            queue_depth = redis_client.zcard(queue_key)

            # Calculate wait times if there are calls
            calls = redis_client.zrange(queue_key, 0, -1)
            wait_times = []
            now = datetime.utcnow()

            for call_json in calls:
                try:
                    call_data = json.loads(call_json)
                    enqueued = datetime.fromisoformat(call_data.get('enqueued_at', now.isoformat()))
                    wait_times.append((now - enqueued).total_seconds())
                except:
                    continue

            avg_wait = sum(wait_times) / len(wait_times) if wait_times else 0
            longest_wait = max(wait_times) if wait_times else 0

            all_status.append({
                'queue_id': queue_id,
                'name': queue_id.capitalize(),
                'depth': queue_depth,
                'average_wait_seconds': int(avg_wait),
                'longest_wait_seconds': int(longest_wait)
            })

        return jsonify(all_status)

    except Exception as e:
        logger.error(f"Error getting all queues status: {str(e)}")
        return jsonify({"error": "Failed to get queues status"}), 500


@queues_bp.route('/mock/clear', methods=['POST'])
@require_auth
def clear_mock_data():
    """
    Clear all mock/demo calls from queues
    """
    try:
        service = QueueService()
        cleared_count = 0

        # Clear demo calls from all queues
        for queue_id in ['sales', 'support', 'billing']:
            queue_key = f"queue:{queue_id}"
            redis_client = service.redis_client

            if redis_client:
                # Get all calls in the queue
                calls = redis_client.zrange(queue_key, 0, -1)

                # Remove only demo/mock calls
                for call_json in calls:
                    try:
                        call_data = json.loads(call_json)
                        call_id = call_data.get('call_id', '')

                        # Check if it's a demo call (starts with demo_ or mock_)
                        if call_id.startswith('demo_') or call_id.startswith('mock_'):
                            redis_client.zrem(queue_key, call_json)
                            cleared_count += 1
                    except Exception as e:
                        logger.warning(f"Error processing call data: {e}")

        logger.info(f"Cleared {cleared_count} mock calls from queues")

        return jsonify({
            'success': True,
            'message': f'Cleared {cleared_count} mock calls from queues',
            'cleared_count': cleared_count
        })

    except Exception as e:
        logger.error(f"Error clearing mock data: {str(e)}")
        return jsonify({'error': str(e)}), 500


@queues_bp.route('/mock/generate', methods=['POST'])
@require_auth
def generate_mock_data():
    """
    Generate mock queue data for demos
    """
    try:
        import random
        import json
        import uuid

        # Try to import Faker, fall back to simple generation if not available
        try:
            from faker import Faker
            fake = Faker()
        except ImportError:
            fake = None

        redis_client = get_redis_client()

        if not redis_client:
            logger.error("Redis client not available")
            return jsonify({"error": "Redis not available"}), 503

        # Clear existing queue data
        for queue_id in ['sales', 'support', 'billing']:
            redis_client.delete(f"queue:{queue_id}")

        # Queue configurations for realistic demo data
        queue_configs = {
            'sales': {
                'min_calls': 3,
                'max_calls': 8,
                'vip_chance': 0.2,
                'reasons': ['Product demo request', 'Pricing inquiry', 'Enterprise upgrade', 'New customer onboarding'],
                'ai_summaries': [
                    'Customer interested in enterprise plan, needs 50+ seats',
                    'Comparing us with Twilio, wants to see AI features',
                    'Existing customer wants to add more agents',
                    'Startup looking for affordable solution'
                ]
            },
            'support': {
                'min_calls': 5,
                'max_calls': 12,
                'vip_chance': 0.15,
                'reasons': ['Technical issue', 'Integration help', 'API question', 'Billing problem', 'Feature request'],
                'ai_summaries': [
                    'WebSocket connection dropping intermittently',
                    'Need help with SWML configuration',
                    'Questions about AI agent capabilities',
                    'Call recording not working properly',
                    'Request for bulk SMS feature'
                ]
            },
            'billing': {
                'min_calls': 2,
                'max_calls': 5,
                'vip_chance': 0.25,
                'reasons': ['Payment failed', 'Invoice question', 'Plan upgrade', 'Refund request'],
                'ai_summaries': [
                    'Credit card declined, needs to update payment method',
                    'Questions about usage charges this month',
                    'Wants to upgrade from Basic to Pro plan',
                    'Requesting refund for accidental double charge'
                ]
            }
        }

        total_calls_generated = 0

        for queue_id, config in queue_configs.items():
            num_calls = random.randint(config['min_calls'], config['max_calls'])

            for i in range(num_calls):
                # Generate realistic wait times (newer calls have shorter wait times)
                wait_minutes = random.uniform(0, 15) * (1 - i/num_calls)

                # Determine priority based on position and randomness
                if i == 0 and random.random() < 0.3:  # First call might be critical
                    priority = 'urgent'
                    priority_score = 1  # For Redis sorting
                elif random.random() < config['vip_chance']:
                    priority = 'high'
                    priority_score = 2  # VIP/High
                elif i < 2:
                    priority = 'high'
                    priority_score = 3
                else:
                    priority = random.choice(['medium', 'medium', 'medium', 'low'])
                    priority_score = 5 if priority == 'medium' else 7

                # Generate customer data
                is_vip = random.random() < config['vip_chance']
                is_returning = random.random() < 0.4

                # Pick reason and AI summary
                reason = random.choice(config['reasons'])
                ai_summary = random.choice(config['ai_summaries'])

                # Generate names and phone numbers
                if fake:
                    customer_name = fake.name()
                    phone_number = fake.phone_number()
                    call_id = f'demo_{queue_id}_{fake.uuid4()[:8]}'
                    account_num = fake.random_number(digits=8) if is_returning else None
                else:
                    # Fallback without Faker
                    first_names = ['John', 'Jane', 'Mike', 'Sarah', 'David', 'Emily', 'Robert', 'Lisa']
                    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller']
                    customer_name = f"{random.choice(first_names)} {random.choice(last_names)}"
                    phone_number = f"+1{random.randint(2000000000, 9999999999)}"
                    call_id = f'demo_{queue_id}_{uuid.uuid4().hex[:8]}'
                    account_num = random.randint(10000000, 99999999) if is_returning else None

                call_data = {
                    'call_id': call_id,
                    'queue_id': queue_id,
                    'priority': priority,
                    'context': {
                        'customer_name': customer_name,
                        'phone_number': phone_number,
                        'reason': reason,
                        'ai_summary': ai_summary,
                        'sentiment': random.choices(
                            ['positive', 'neutral', 'negative'],
                            weights=[0.3, 0.5, 0.2]
                        )[0],
                        'is_vip': is_vip,
                        'is_returning': is_returning,
                        'confidence_score': random.uniform(0.75, 0.98),
                        'extracted_info': {
                            'account_number': account_num,
                            'product_tier': random.choice(['Basic', 'Pro', 'Enterprise']) if is_returning else None,
                            'monthly_spend': random.randint(100, 5000) if is_vip else None
                        },
                        'ai_actions': [
                            {'action': 'greeting', 'result': 'completed'},
                            {'action': 'identity_verification', 'result': 'completed'},
                            {'action': 'issue_categorization', 'result': reason}
                        ]
                    },
                    'caller_info': {
                        'number': phone_number,
                        'name': customer_name
                    }
                }

                # Enqueue the call directly to Redis
                queue_key = f"queue:{queue_id}"

                # Add enqueued_at timestamp
                call_data['enqueued_at'] = datetime.utcnow().isoformat()

                # Add to Redis sorted set with priority_score as score
                redis_client.zadd(queue_key, {json.dumps(call_data): priority_score})

                total_calls_generated += 1

        # Generate some agent status data
        agent_statuses = {
            'agent_sarah': {'status': 'busy', 'current_call': 'call_123', 'queue': 'sales'},
            'agent_john': {'status': 'available', 'queue': 'support'},
            'agent_emily': {'status': 'after-call', 'queue': 'billing'},
            'agent_mike': {'status': 'available', 'queue': 'support'},
            'agent_lisa': {'status': 'break', 'queue': 'sales'}
        }

        for agent_id, status_data in agent_statuses.items():
            redis_client.hset(f'agent:{agent_id}', mapping={
                'status': status_data['status'],
                'last_update': datetime.utcnow().isoformat(),
                'queue': status_data.get('queue', 'general'),
                'current_call': status_data.get('current_call', '')
            })

        # Broadcast the update via WebSocket
        from app.services.callcenter_socketio import broadcast_queue_updates
        broadcast_queue_updates()

        logger.info(f"Generated {total_calls_generated} mock calls across queues")

        # Get queue depths for response
        queue_depths = {}
        for queue_id in queue_configs.keys():
            queue_key = f"queue:{queue_id}"
            depth = redis_client.zcard(queue_key)
            queue_depths[queue_id] = depth

        return jsonify({
            'success': True,
            'message': f'Generated {total_calls_generated} mock calls for demo',
            'queues': queue_depths
        })

    except Exception as e:
        logger.error(f"Error generating mock data: {str(e)}")
        return jsonify({"error": f"Failed to generate mock data: {str(e)}"}), 500