"""Keep Fabric SWML Webhook resources in sync with EXTERNAL_URL.

When ngrok rotates, the SWML Webhook resources in the SignalWire Dashboard
still point at the previous URL and SWML requests 404. This module finds the
managed webhook(s) by their Call Fabric address name and PATCHes the
`primary_request_url` to match the current EXTERNAL_URL.

Runs at backend startup (via create_app) and can also be triggered on-demand
via POST /api/admin/fabric/sync-webhooks.
"""

import logging
import os
from typing import Optional

from app.services import signalwire_client as sw_client

logger = logging.getLogger(__name__)


def _extract_address_name(fabric_address: str) -> Optional[str]:
    """'/public/agent-conference-swml' -> 'agent-conference-swml'."""
    if not fabric_address:
        return None
    parts = [p for p in fabric_address.strip().strip('/').split('/') if p]
    return parts[-1] if parts else None


def _find_webhook_by_address_name(client, address_name: str) -> Optional[dict]:
    """Find the SWML Webhook resource whose Call Fabric address matches.

    Iterates swml_webhook resources and checks each one's addresses for a
    name match. None if not found.
    """
    resp = client.fabric.swml_webhooks.list(page_size=100)
    webhooks = resp.get('data', []) if isinstance(resp, dict) else resp
    for wh in webhooks:
        try:
            addrs_resp = client.fabric.swml_webhooks.list_addresses(wh['id'])
        except Exception as e:
            logger.debug(f"list_addresses({wh.get('id')}) failed: {e}")
            continue
        addrs = addrs_resp.get('data', []) if isinstance(addrs_resp, dict) else addrs_resp
        for a in addrs:
            if a.get('name') == address_name:
                return wh
    return None


def sync_agent_conference_webhook(external_url: str) -> dict:
    """Point the agent-conference-swml Fabric webhook at EXTERNAL_URL.

    Returns a dict describing the outcome (suitable for logging + API responses):
        {'ok': True, 'webhook_id': '...', 'url': '...', 'previous': '...'}
        {'ok': True, 'unchanged': True, ...} if URL already matched
        {'skipped': True, 'reason': ...}     if preconditions unmet
        {'error': 'message'}                 on failure
    """
    if not external_url:
        return {'skipped': True, 'reason': 'EXTERNAL_URL not set'}
    if not sw_client.is_configured():
        return {'skipped': True, 'reason': 'SignalWire credentials not configured'}

    fabric_address = os.getenv('AGENT_CONFERENCE_RESOURCE', '/public/agent-conference-swml')
    address_name = _extract_address_name(fabric_address)
    if not address_name:
        return {'error': f'AGENT_CONFERENCE_RESOURCE malformed: {fabric_address!r}'}

    target_url = f"{external_url.rstrip('/')}/api/conferences/agent-conference"

    try:
        client = sw_client.get_client()
        webhook = _find_webhook_by_address_name(client, address_name)
        if not webhook:
            return {
                'error': (
                    f'No SWML webhook with address "{address_name}" found in this space. '
                    f'Create it via the Dashboard and set AGENT_CONFERENCE_RESOURCE env var.'
                )
            }

        # The real fields live under the nested `swml_webhook` dict, same shape
        # as subscribers (inspected during the phone-number rewrite).
        current = (webhook.get('swml_webhook') or {}).get('primary_request_url')
        webhook_id = webhook['id']

        if current and current.rstrip('/') == target_url.rstrip('/'):
            return {
                'ok': True,
                'unchanged': True,
                'webhook_id': webhook_id,
                'url': current,
            }

        client.fabric.swml_webhooks.update(webhook_id, primary_request_url=target_url)
        logger.info(
            f"[fabric_sync] Updated '{address_name}' ({webhook_id}) "
            f"primary_request_url → {target_url}"
        )
        return {
            'ok': True,
            'webhook_id': webhook_id,
            'url': target_url,
            'previous': current,
            'address_name': address_name,
        }
    except sw_client.SignalWireRestError as e:
        logger.error(f"[fabric_sync] SignalWire API error: {e}")
        return {'error': f'SignalWire API error: {e}'}
    except Exception as e:
        logger.exception("[fabric_sync] Unexpected failure")
        return {'error': str(e)}


def sync_all(external_url: str) -> dict:
    """Run every configured webhook sync. Extend this as more resources are added."""
    return {
        'agent_conference': sync_agent_conference_webhook(external_url),
    }
