"""Shared SignalWire REST client.

Wraps signalwire.rest.RestClient so callers never have to rebuild auth, paths,
or resource URLs by hand. Reads credentials from the standard environment
variables the SDK expects (SIGNALWIRE_PROJECT_ID, SIGNALWIRE_API_TOKEN,
SIGNALWIRE_SPACE).
"""

import logging
import os
from threading import Lock
from typing import Optional

from signalwire.rest import RestClient, SignalWireRestError

logger = logging.getLogger(__name__)

_client: Optional[RestClient] = None
_lock = Lock()


def get_client() -> RestClient:
    """Return a process-wide shared RestClient.

    Constructed lazily so import-time environments without credentials
    (tests, migrations) don't fail on module load.
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = RestClient(
                project=os.getenv("SIGNALWIRE_PROJECT_ID"),
                token=os.getenv("SIGNALWIRE_API_TOKEN"),
                host=os.getenv("SIGNALWIRE_SPACE"),
            )
    return _client


def is_configured() -> bool:
    return bool(
        os.getenv("SIGNALWIRE_PROJECT_ID")
        and os.getenv("SIGNALWIRE_API_TOKEN")
        and os.getenv("SIGNALWIRE_SPACE")
    )


__all__ = ["get_client", "is_configured", "SignalWireRestError"]
