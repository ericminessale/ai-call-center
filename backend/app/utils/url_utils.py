"""URL utilities for handling external URLs and proxies."""
import os
from flask import request

# External URL for SignalWire callbacks (e.g., ngrok URL)
# Set this in .env when developing locally so SignalWire can reach your server
EXTERNAL_URL = os.getenv('EXTERNAL_URL')


def get_base_url():
    """Get the base URL for callbacks, handling proxy headers.

    Priority:
    1. EXTERNAL_URL environment variable (for local dev with ngrok)
    2. X-Forwarded-Host header (when behind ngrok/proxy)
    3. request.host_url (fallback)

    Usage:
        Add EXTERNAL_URL=https://your-ngrok-url.ngrok.io to your .env file
        when developing locally with ngrok.
    """
    # If EXTERNAL_URL is set, always use it
    if EXTERNAL_URL:
        return EXTERNAL_URL.rstrip('/')

    forwarded_host = request.headers.get('X-Forwarded-Host')
    forwarded_proto = request.headers.get('X-Forwarded-Proto', 'https')

    if forwarded_host:
        if 'ngrok' in forwarded_host:
            forwarded_proto = 'https'
        return f"{forwarded_proto}://{forwarded_host}"
    else:
        return request.host_url.rstrip('/')
