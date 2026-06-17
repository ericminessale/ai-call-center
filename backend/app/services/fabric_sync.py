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
import re
from typing import Optional
from urllib.parse import urlparse

from app.services import signalwire_client as sw_client

logger = logging.getLogger(__name__)


# Phone-number webhook URL paths we manage. After an ngrok rotation we re-point
# any phone number whose path matches one of these to the new EXTERNAL_URL,
# preserving the queue slug suffix so routing mode (ai_triage / ai_specialist /
# human_direct) stays intact. URLs with paths NOT in this list are owned by
# someone else (other apps, manual experiments) and are left untouched.
_MANAGED_PHONE_PATH_PATTERNS = (
    re.compile(r'^/api/swml/initial-call/?$'),
    re.compile(r'^/api/swml/ai-specialist/[^/?]+/?$'),
    re.compile(r'^/api/queues/[^/?]+/direct-inbound/?$'),
)


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


def _friendly_name_for_route(url: str) -> Optional[str]:
    """Turn one of our managed webhook URLs into a human-readable Calling
    Handler name for the SignalWire Dashboard. Without this, the resource's
    display_name defaults to the full ngrok URL — ugly + unstable. Returns
    None for URLs we don't recognize (leave them as-is)."""
    if not url:
        return None
    try:
        path = urlparse(url).path or ''
    except Exception:
        return None
    if path.startswith('/api/swml/initial-call'):
        return 'AI Receptionist (initial call)'
    if path.startswith('/api/swml/ai-specialist/'):
        slug = path.split('/api/swml/ai-specialist/', 1)[1].strip('/').split('/', 1)[0]
        if slug:
            return f'AI Specialist — {slug.capitalize()}'
        return 'AI Specialist'
    if path.startswith('/api/queues/') and path.endswith('/direct-inbound'):
        slug = path[len('/api/queues/'):-len('/direct-inbound')].strip('/')
        if slug:
            return f'{slug.capitalize()} — Direct inbound'
        return 'Direct inbound'
    if path.startswith('/api/swml/out-of-service'):
        return 'Out of service (unassigned)'
    return None


def update_swml_webhook_for_phone(
    *,
    calling_handler_resource_id: str,
    primary_request_url: str,
    status_callback_url: str,
) -> dict:
    """Update the swml_webhook Fabric resource bound to a phone number.

    The phone_numbers REST API stores ``call_status_callback_url`` for
    display, but the runtime field SignalWire actually fires when a call
    completes lives on the per-phone ``swml_webhook`` Fabric resource
    (the Dashboard's "Calling Handler"). Setting it via phone_numbers.update
    does NOT reliably propagate — empirical: the user can manually edit
    the Calling Handler in the dashboard and start getting webhooks, while
    a REST update to phone_numbers (even with the same URL) doesn't.

    The correct API path is ``PATCH /api/fabric/resources/swml_webhooks/{id}``
    — note the ``/resources/`` segment that's easy to miss. The SDK
    namespace ``client.fabric.swml_webhooks`` uses this path.

    Idempotent — safe to call after every phone_numbers.update.
    """
    if not calling_handler_resource_id:
        return {'skipped': True, 'reason': 'no calling_handler_resource_id'}

    # Derive a human-readable name from the route so the Dashboard shows
    # "Support — Direct inbound" instead of the raw ngrok URL.
    friendly_name = _friendly_name_for_route(primary_request_url)

    try:
        client = sw_client.get_client()
        update_kwargs = {
            'primary_request_url': primary_request_url,
            'status_callback_url': status_callback_url,
        }
        if friendly_name:
            update_kwargs['name'] = friendly_name
            update_kwargs['display_name'] = friendly_name
        result = client.fabric.swml_webhooks.update(
            calling_handler_resource_id,
            **update_kwargs,
        )
        inner = (result.get('swml_webhook') or {}) if isinstance(result, dict) else {}
        return {
            'ok': True,
            'webhook_id': calling_handler_resource_id,
            'primary_request_url': inner.get('primary_request_url'),
            'status_callback_url': inner.get('status_callback_url'),
        }
    except Exception as e:
        logger.error(
            f"[fabric_sync] failed to update swml_webhook "
            f"{calling_handler_resource_id}: {e}"
        )
        return {'ok': False, 'error': str(e)}


def sync_phone_number_webhooks(external_url: str) -> dict:
    """Re-point managed phone-number SWML webhooks at the current EXTERNAL_URL.

    For each phone number whose ``call_relay_script_url`` path matches one of
    our routes (initial-call / ai-specialist/<slug> / queues/<slug>/direct-inbound),
    swap the host portion to ``external_url``. The path + query are preserved so
    the per-number routing mode survives ngrok rotations without manual reassign.

    Phone numbers pointing at unmanaged URLs (other apps, external SWML scripts,
    Twilio-compat cXML) are left alone — we don't touch what we don't own.
    """
    if not external_url:
        return {'skipped': True, 'reason': 'EXTERNAL_URL not set'}
    if not sw_client.is_configured():
        return {'skipped': True, 'reason': 'SignalWire credentials not configured'}

    target_host = external_url.rstrip('/')

    # Phone-number-level call-state callback. Without this, SignalWire never
    # tells us when a caller hangs up while parked in our SWML (in-line goto/
    # label hold loop, AI script, etc.) — the Call row stays at status='waiting'
    # forever and the queue UI shows ghost callers indefinitely. The SWML
    # `set` verb's `call_state_url` is supposed to do the same thing per-call
    # but in practice we've seen it not fire reliably; configuring it at the
    # phone-number level guarantees every inbound leg gets its lifecycle
    # events delivered to /api/webhooks/call-status. The handler there
    # cleans up Redis + Conference rows + emits `queue_update action='ended'`.
    from app.utils.url_utils import signed_webhook_url
    desired_status_callback = signed_webhook_url(
        f"{target_host}/api/webhooks/call-status"
    )

    try:
        client = sw_client.get_client()
        resp = client.phone_numbers.list(page_size=100)
        numbers = resp.get('data', []) if isinstance(resp, dict) else resp
    except Exception as e:
        logger.error(f"[fabric_sync] phone-numbers list failed: {e}")
        return {'error': f'Failed to list phone numbers: {e}'}

    synced = []
    errors = []
    unchanged = 0
    skipped_unmanaged = 0

    for n in numbers:
        sid = n.get('id')
        current_url = n.get('call_relay_script_url') or ''
        if not current_url:
            continue

        parsed = urlparse(current_url)
        if not parsed.path or not any(p.match(parsed.path) for p in _MANAGED_PHONE_PATH_PATTERNS):
            skipped_unmanaged += 1
            continue

        new_url = target_host + parsed.path
        if parsed.query:
            new_url += '?' + parsed.query

        # Did any of the fields drift? Check both the legacy phone_numbers
        # fields AND the swml_webhook fields (the latter is what SignalWire
        # actually reads at runtime — the legacy view often lies). Without
        # the swml_webhook check, fabric_sync skips updates when only the
        # status_callback_url has drifted on the swml_webhook (a common
        # failure mode after ngrok rotation, since the swml_webhook's
        # status_callback_url isn't auto-synced from the phone_numbers view).
        current_status_cb = n.get('call_status_callback_url') or ''
        script_url_drifted = current_url.rstrip('/') != new_url.rstrip('/')
        status_cb_drifted = current_status_cb.rstrip('/') != desired_status_callback.rstrip('/')

        handler_id = n.get('calling_handler_resource_id')
        swml_wh_drifted = False
        if handler_id:
            try:
                wh = client.fabric.swml_webhooks.get(handler_id)
                wh_inner = (wh.get('swml_webhook') or {}) if isinstance(wh, dict) else {}
                wh_primary = (wh_inner.get('primary_request_url') or '').rstrip('/')
                wh_status_cb = (wh_inner.get('status_callback_url') or '').rstrip('/')
                wh_name = wh_inner.get('name') or ''
                desired_name = _friendly_name_for_route(new_url) or ''
                if wh_primary != new_url.rstrip('/'):
                    swml_wh_drifted = True
                if wh_status_cb != desired_status_callback.rstrip('/'):
                    swml_wh_drifted = True
                # If the resource's display name still looks like a raw URL
                # (or otherwise differs from the friendly route name), patch
                # it on the next sync. Without this the Dashboard shows the
                # ngrok URL as the handler's name — ugly and unstable.
                if desired_name and wh_name != desired_name:
                    swml_wh_drifted = True
            except Exception as e:
                logger.warning(
                    f"[fabric_sync] swml_webhook drift check failed for {handler_id}: {e}"
                )

        if not (script_url_drifted or status_cb_drifted or swml_wh_drifted):
            unchanged += 1
            continue

        try:
            client.phone_numbers.update(
                sid,
                call_handler='relay_script',
                call_relay_script_url=new_url,
                call_status_callback_url=desired_status_callback,
            )

            # The phone_numbers update above keeps the legacy view in sync,
            # but the field SignalWire's runtime actually reads for status
            # callbacks lives on the swml_webhook Fabric resource (the
            # Dashboard's "Calling Handler"). Update it explicitly — this
            # is THE fix for "I hang up and the call stays in the queue".
            handler_id = n.get('calling_handler_resource_id')
            swml_wh_result = update_swml_webhook_for_phone(
                calling_handler_resource_id=handler_id,
                primary_request_url=new_url,
                status_callback_url=desired_status_callback,
            )

            synced.append({
                'number': n.get('number') or n.get('phone_number'),
                'sid': sid,
                'previous': current_url,
                'url': new_url,
                'status_callback': desired_status_callback,
                'status_callback_previous': current_status_cb,
                'swml_webhook_update': swml_wh_result,
            })
            logger.info(
                f"[fabric_sync] phone {n.get('number')} ({sid}): "
                f"script_url={current_url} → {new_url}; "
                f"status_callback={current_status_cb or '(none)'} → {desired_status_callback}; "
                f"swml_webhook={handler_id} → {swml_wh_result.get('ok')}"
            )
        except Exception as e:
            errors.append({'sid': sid, 'error': str(e)})
            logger.error(f"[fabric_sync] failed to sync phone {sid}: {e}")

    return {
        'ok': len(errors) == 0,
        'synced_count': len(synced),
        'unchanged': unchanged,
        'skipped_unmanaged': skipped_unmanaged,
        'synced': synced,
        'errors': errors,
    }


def sync_all(external_url: str) -> dict:
    """Run every configured webhook sync. Extend this as more resources are added."""
    return {
        'agent_conference': sync_agent_conference_webhook(external_url),
        'phone_numbers': sync_phone_number_webhooks(external_url),
    }
