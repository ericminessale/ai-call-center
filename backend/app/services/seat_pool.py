"""
Boot-time provisioning of the SignalWire subscriber seat pool (TENANCY_MODE).

Replaces demo_seed.py's persona provisioning. Runs once per boot (Redis
NX lock in create_app, same pattern as fabric_sync) and is idempotent:

1. Ensure ``SEAT_POOL_SIZE`` seat rows exist. Seat emails reuse the old
   persona convention (``demo{NN}@demo.invalid``) ON PURPOSE — the
   link-existing-by-email path below then ADOPTS the SignalWire-side
   subscribers the demo already provisioned, so a rebuilt database
   (docker compose down -v) re-links the space's existing subscribers
   instead of minting 20 duplicates.
2. Create-or-link a SignalWire Subscriber for every seat missing one
   (ported from demo_seed._provision_one_demo_subscriber, including its
   nested-``subscriber``-key response handling).
3. Resolve the REAL fabric address for any seat whose address is missing
   or still the old fabricated ``/private/agent-<id>`` form. The platform
   auto-materializes one address per subscriber (slugged from
   display_name); fabricated strings were never registered and fail as
   SWML connect targets — seats must hold the platform's answer, resolved
   via ``list_addresses`` (same as api/fabric.py).

Failures are per-seat and non-fatal — a partially provisioned pool still
serves however many seats it has.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime

from app import db
from app.models import SubscriberSeat
from app.services import signalwire_client as sw_client

logger = logging.getLogger(__name__)


def seat_pool_size() -> int:
    """Target number of seats. ``SEAT_POOL_SIZE`` overrides; falls back to
    the old ``DEMO_POOL_SIZE`` knob, then 20. Clamped to [1, 100]."""
    raw = (os.getenv('SEAT_POOL_SIZE') or os.getenv('DEMO_POOL_SIZE') or '20').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 20
    return max(1, min(n, 100))


def _seat_email(slot: int) -> str:
    """Canonical seat email for slot N (1-indexed) — matches the old
    persona convention so existing SignalWire subscribers get adopted."""
    return f'demo{slot:02d}@demo.invalid'


def _seat_display_name(slot: int) -> str:
    return f'Demo Agent {slot:02d}'


def ensure_seat_pool() -> dict:
    """Top up seat rows, provision/link subscribers, resolve addresses.
    Idempotent; returns a summary dict for boot logging."""
    created_rows = _ensure_seat_rows(seat_pool_size())

    if not sw_client.is_configured():
        logger.warning(
            "seat_pool: SignalWire client not configured — seats exist but "
            "have no subscribers; WebRTC token minting will 503 until "
            "credentials are set."
        )
        return {'seat_rows_created': created_rows, 'skipped': 'sw_client not configured'}

    provisioned = 0
    linked = 0
    failed = []
    for seat in SubscriberSeat.query.filter(
        SubscriberSeat.signalwire_subscriber_id.is_(None)
    ).all():
        try:
            mode = _provision_seat_subscriber(seat)
            if mode == 'created':
                provisioned += 1
            else:
                linked += 1
            logger.info("seat_pool: %s subscriber %s for %s",
                        mode, seat.signalwire_subscriber_id, seat.email)
        except Exception as exc:
            failed.append((seat.email, str(exc)))
            logger.warning("seat_pool: provisioning failed for %s: %s", seat.email, exc)

    resolved = 0
    for seat in SubscriberSeat.query.filter(
        SubscriberSeat.signalwire_subscriber_id.isnot(None)
    ).all():
        if not seat.address_needs_resolution():
            continue
        address = _resolve_real_address(seat.signalwire_subscriber_id)
        if address:
            seat.signalwire_address = address
            resolved += 1
    if resolved:
        db.session.commit()

    return {
        'seat_rows_created': created_rows,
        'subscribers_provisioned': provisioned,
        'subscribers_linked_existing': linked,
        'addresses_resolved': resolved,
        'failed': len(failed),
        'failed_detail': failed[:5],
    }


def _ensure_seat_rows(target: int) -> int:
    created = 0
    for slot in range(1, target + 1):
        email = _seat_email(slot)
        if SubscriberSeat.query.filter_by(email=email).first():
            continue
        db.session.add(SubscriberSeat(email=email, display_name=_seat_display_name(slot)))
        created += 1
    if created:
        db.session.commit()
    return created


def _provision_seat_subscriber(seat) -> str:
    """Create or adopt the SignalWire Subscriber for one seat.

    Returns 'created' or 'linked_existing'. Raises on hard failure.
    Adoption resets the subscriber password (earlier owners of the
    credential — old persona rows or failed runs — lost it)."""
    from app.api.fabric import _find_subscriber_by_email

    password = secrets.token_urlsafe(32)

    existing = _find_subscriber_by_email(seat.email)
    if existing:
        subscriber_id = existing.get('id')
        nested = existing.get('subscriber') or existing
        try:
            sw_client.get_client().fabric.subscribers.update(
                subscriber_id, password=password,
            )
        except sw_client.SignalWireRestError as e:
            raise Exception(f"Failed to reset password on existing subscriber: {e}")
        mode = 'linked_existing'
    else:
        client = sw_client.get_client()
        display_name = seat.display_name or seat.email
        payload = {
            'email': seat.email,
            'password': password,
            'first_name': display_name.split()[0],
            'last_name': display_name.split()[-1] if len(display_name.split()) > 1 else 'Agent',
            'display_name': display_name,
            'job_title': 'Call Center Demo Agent',
            'metadata': {
                'seat_id': seat.id,
                'pool': 'seat',
            },
        }
        try:
            resp = client.fabric.subscribers.create(**payload)
        except sw_client.SignalWireRestError as e:
            raise Exception(f"create subscriber failed: {e}")
        subscriber_id = resp.get('id')
        nested = resp.get('subscriber') or {}
        if not subscriber_id:
            raise Exception(f"Subscriber response missing id: {resp}")
        mode = 'created'

    reference = nested.get('email') or seat.email

    seat.signalwire_subscriber_id = subscriber_id
    seat.signalwire_username = reference
    seat.set_subscriber_password(password)
    seat.signalwire_address = _resolve_real_address(subscriber_id)
    seat.provisioned_at = datetime.utcnow()
    db.session.commit()
    return mode


def _resolve_real_address(subscriber_id) -> str | None:
    """Ask the platform for the subscriber's auto-materialized fabric
    address (``/private/<slug>``). Returns None when unavailable — the
    seat then stays flagged for re-resolution on the next boot."""
    try:
        resp = sw_client.get_client().fabric.subscribers.list_addresses(subscriber_id)
        addr_list = resp.get('data', []) if isinstance(resp, dict) else resp
        if addr_list:
            name = addr_list[0].get('name')
            if name:
                return f'/private/{name}'
        logger.warning(
            "seat_pool: subscriber %s has no addresses yet — will re-resolve next boot",
            subscriber_id,
        )
    except Exception as exc:
        logger.warning("seat_pool: address resolution failed for %s: %s", subscriber_id, exc)
    return None
