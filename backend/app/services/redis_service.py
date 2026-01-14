import redis
import json
import logging
import threading
from flask import current_app

logger = logging.getLogger(__name__)


def get_redis_client():
    """Get Redis client instance with fallback."""
    from app import redis_client

    # If the global client exists and can ping, use it
    if redis_client:
        try:
            redis_client.ping()
            return redis_client
        except:
            pass

    # Try to create a new connection with IP fallback
    import redis
    try:
        # Try hostname first
        client = redis.from_url('redis://redis:6379/0', decode_responses=True)
        client.ping()
        return client
    except:
        try:
            # Fallback to IP
            client = redis.from_url('redis://172.18.0.3:6379/0', decode_responses=True)
            client.ping()
            return client
        except:
            return None


def publish_event(channel, data):
    """Publish an event to a Redis channel in a non-blocking way."""
    def _publish():
        try:
            client = get_redis_client()
            if client:
                message = json.dumps(data)
                client.publish(channel, message)
                logger.debug(f"Published to {channel}: {message}")
        except Exception as e:
            # Log at debug level to reduce noise - Redis pub/sub is optional
            logger.debug(f"Redis publish failed (non-critical): {str(e)}")

    # Run in background thread to avoid blocking webhook response
    thread = threading.Thread(target=_publish)
    thread.daemon = True  # Daemon thread won't prevent app shutdown
    thread.start()


def subscribe_to_channel(channel):
    """Subscribe to a Redis channel."""
    try:
        client = get_redis_client()
        if client:
            pubsub = client.pubsub()
            pubsub.subscribe(channel)
            return pubsub
    except Exception as e:
        logger.error(f"Failed to subscribe to Redis channel: {str(e)}")
        return None


def set_cache(key, value, expiry=3600):
    """Set a value in Redis cache with expiry."""
    try:
        client = get_redis_client()
        if client:
            if isinstance(value, dict):
                value = json.dumps(value)
            client.setex(key, expiry, value)
            return True
    except Exception as e:
        logger.error(f"Failed to set cache: {str(e)}")
        return False


def get_cache(key):
    """Get a value from Redis cache."""
    try:
        client = get_redis_client()
        if client:
            value = client.get(key)
            if value:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
    except Exception as e:
        logger.error(f"Failed to get cache: {str(e)}")
    return None


def delete_cache(key):
    """Delete a value from Redis cache."""
    try:
        client = get_redis_client()
        if client:
            return client.delete(key) > 0
    except Exception as e:
        logger.error(f"Failed to delete cache: {str(e)}")
    return False


def add_to_set(set_name, value):
    """Add a value to a Redis set."""
    try:
        client = get_redis_client()
        if client:
            return client.sadd(set_name, value) > 0
    except Exception as e:
        # Log at debug level to reduce noise
        logger.debug(f"Redis operation failed (non-critical): {str(e)}")
    return False


def remove_from_set(set_name, value):
    """Remove a value from a Redis set."""
    try:
        client = get_redis_client()
        if client:
            return client.srem(set_name, value) > 0
    except Exception as e:
        # Log at debug level to reduce noise
        logger.debug(f"Redis operation failed (non-critical): {str(e)}")
    return False


def get_set_members(set_name):
    """Get all members of a Redis set."""
    try:
        client = get_redis_client()
        if client:
            return list(client.smembers(set_name))
    except Exception as e:
        logger.error(f"Failed to get set members: {str(e)}")
    return []


def increment_counter(key, amount=1):
    """Increment a counter in Redis."""
    try:
        client = get_redis_client()
        if client:
            return client.incrby(key, amount)
    except Exception as e:
        logger.error(f"Failed to increment counter: {str(e)}")
    return 0