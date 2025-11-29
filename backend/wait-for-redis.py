#!/usr/bin/env python3
"""Wait for Redis to be available before starting the app."""

import time
import sys
import redis
import os

def wait_for_redis(max_attempts=30):
    """Wait for Redis to become available."""
    redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')

    for attempt in range(max_attempts):
        try:
            client = redis.from_url(redis_url)
            client.ping()
            print(f"✓ Redis is available at {redis_url}")
            return True
        except Exception as e:
            print(f"Waiting for Redis... attempt {attempt + 1}/{max_attempts}")
            time.sleep(2)

    print(f"✗ Redis not available after {max_attempts} attempts")
    return False

if __name__ == "__main__":
    if wait_for_redis():
        sys.exit(0)
    else:
        sys.exit(1)