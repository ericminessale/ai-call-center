"""
Idempotent demo-persona seeding for the hosted demo deployment.

Runs at backend boot when ``DEMO_MODE=true``. Two phases:

1. ``User`` rows — N (default 20) rows with ``role='demo_agent'`` and
   persona names that look like real teammates ("Alex Sales", "Jordan
   Support", etc.).
2. SignalWire Subscribers — one permanent Subscriber per persona,
   created via the existing ``_create_permanent_subscriber()`` helper
   in ``app.api.fabric``. The Subscriber ID + encrypted password get
   stored back on the User row.

Both phases are idempotent on every dimension:
  - if rows already exist, do nothing
  - if a partial set exists, top up to ``DEMO_POOL_SIZE``
  - never modifies or deletes existing rows (operator-edited names
    stay; subscriber IDs once linked are not re-created)

NOT a migration — pool size is configurable via env, and we don't want
prod clones running this seed accidentally. Migrations always run; this
runs only when the env flag says so.

If SignalWire credentials are missing or the API is unreachable, phase
2 is skipped with a loud warning. The User rows still seed; the lease
layer can either refuse leases (the persona has no subscriber to back a
WebRTC token) or surface a clear "demo not yet provisioned" error.
"""

from __future__ import annotations

import logging
import secrets

from app import db
from app.models import User
from app.services import signalwire_client as sw_client
from app.utils.demo_config import DEMO_AGENT_ROLE, demo_pool_size, is_demo_mode

logger = logging.getLogger(__name__)


# 20 plausibly-named demo personas. Order is stable so seed slot N
# always maps to the same persona — useful for debugging and for the
# UI's persona-display logic in M2.
_PERSONA_NAMES = [
    'Alex Sales',     'Jordan Support',  'Sam Receptionist', 'Riley Service',
    'Casey Account',  'Morgan Help',     'Avery Concierge',  'Quinn Triage',
    'Skylar Sales',   'Reese Support',   'Drew Service',     'Parker Account',
    'Jamie Help',     'Charlie Sales',   'Finley Support',   'Sage Service',
    'River Account',  'Phoenix Help',    'Rowan Sales',      'Emerson Support',
]


def _persona_email(slot: int) -> str:
    """Return the canonical demo persona email for slot N (1-indexed).

    Uses the ``demo.invalid`` TLD per RFC 2606 so nothing accidentally
    tries to send mail to these. The address is purely an internal key
    — visitors never see it.
    """
    return f'demo{slot:02d}@demo.invalid'


def _persona_name(slot: int) -> str:
    """Pick a stable persona name for the given slot.

    Wraps around the persona list if pool size exceeds the named set.
    """
    return _PERSONA_NAMES[(slot - 1) % len(_PERSONA_NAMES)]


def seed_demo_personas() -> dict:
    """Top up the demo persona pool to the configured size + provision
    SignalWire Subscribers for any persona missing one. Idempotent.

    Returns a small dict for logging — caller logs at INFO level.
    """
    if not is_demo_mode():
        return {'skipped': 'DEMO_MODE not set'}

    target = demo_pool_size()
    rows_created = _seed_user_rows(target)
    subscribers = _provision_subscribers_for_personas()
    return {
        'pool_size': target,
        'rows_created': rows_created,
        **subscribers,
    }


def _seed_user_rows(target: int) -> int:
    """Phase 1: ensure ``target`` User rows exist with role=demo_agent.

    Returns count newly created (zero on a no-op run).
    """
    existing_count = User.query.filter_by(role=DEMO_AGENT_ROLE).count()
    if existing_count >= target:
        return 0

    created = 0
    for slot in range(1, target + 1):
        email = _persona_email(slot)
        if User.query.filter_by(email=email).first():
            continue
        persona = User(
            email=email,
            name=_persona_name(slot),
            role=DEMO_AGENT_ROLE,
            is_active=True,
            languages=['en-US'],
            permissions={},
        )
        # No one ever logs in as these via password; the hash is just
        # there to satisfy the NOT NULL constraint on password_hash.
        # The lease flow issues JWTs directly via /api/demo/start.
        persona.set_password(secrets.token_urlsafe(32))
        db.session.add(persona)
        created += 1

    if created:
        db.session.commit()
    return created


def _provision_subscribers_for_personas() -> dict:
    """Phase 2: ensure each demo persona has a linked SignalWire
    Subscriber. Skipped (with a loud warning) if SignalWire isn't
    configured. Failures on individual personas don't block the rest.

    NOTE: We use a custom create helper here rather than the existing
    ``app.api.fabric._create_permanent_subscriber`` because the latter
    has a pre-existing bug parsing the SignalWire API response shape
    (it expects ``email`` at the top level but the API nests it under
    ``subscriber.email``). That bug affects real-user provisioning too
    but isolating the fix here keeps the demo work scoped.
    """
    if not sw_client.is_configured():
        logger.warning(
            "demo_seed: SignalWire client not configured — skipping "
            "subscriber provisioning. Personas exist but have no "
            "WebRTC capability until credentials are set."
        )
        return {'subscribers_provisioned': 0, 'subscribers_skipped': 'sw_client not configured'}

    needs_subscriber = (
        User.query
        .filter_by(role=DEMO_AGENT_ROLE)
        .filter(User.signalwire_subscriber_id.is_(None))
        .all()
    )
    if not needs_subscriber:
        return {'subscribers_provisioned': 0, 'subscribers_already': True}

    provisioned = 0
    linked_existing = 0
    failed: list[tuple[str, str]] = []
    for persona in needs_subscriber:
        try:
            mode = _provision_one_demo_subscriber(persona)
            if mode == 'created':
                provisioned += 1
            elif mode == 'linked_existing':
                linked_existing += 1
            logger.info(
                "demo_seed: %s subscriber %s for %s",
                mode, persona.signalwire_subscriber_id, persona.email,
            )
        except Exception as exc:
            failed.append((persona.email, str(exc)))
            logger.warning(
                "demo_seed: subscriber provisioning failed for %s: %s",
                persona.email, exc,
            )

    return {
        'subscribers_provisioned': provisioned,
        'subscribers_linked_existing': linked_existing,
        'subscribers_failed': len(failed),
        'subscribers_failed_detail': failed[:5],  # cap log noise
    }


def _provision_one_demo_subscriber(persona) -> str:
    """Create or link a SignalWire Subscriber for a single demo persona.

    Returns ``'created'`` for a fresh create, ``'linked_existing'`` if
    we found a pre-existing subscriber with this email and linked it
    instead. Raises on hard failures.

    The "link existing" path covers two cases:
      1. an earlier seed run created the subscriber but failed to
         persist the link on our side (orphan recovery)
      2. an operator pre-created subscribers via the SignalWire
         dashboard with our naming convention

    Both should be transparent — the caller just sees a working
    persona afterwards.
    """
    from app.api.fabric import _find_subscriber_by_email
    from datetime import datetime

    password = secrets.token_urlsafe(32)
    fabric_address_name = f"agent-{persona.id}"

    # Check first for an existing subscriber with this email — handles
    # orphans from earlier failed runs without re-creating.
    existing = _find_subscriber_by_email(persona.email)
    subscriber_id = None
    nested = None
    mode = 'created'

    if existing:
        subscriber_id = existing.get('id')
        nested = existing.get('subscriber') or existing
        # Reset password so we know what it is — old runs lost it.
        try:
            sw_client.get_client().fabric.subscribers.update(
                subscriber_id, password=password,
            )
        except sw_client.SignalWireRestError as e:
            raise Exception(f"Failed to reset password on existing subscriber: {e}")
        mode = 'linked_existing'
    else:
        client = sw_client.get_client()
        payload = {
            'email': persona.email,
            'password': password,
            'first_name': persona.name.split()[0] if persona.name else 'Demo',
            'last_name': persona.name.split()[-1] if persona.name and len(persona.name.split()) > 1 else 'Agent',
            'display_name': persona.name or f'Demo {persona.id}',
            'job_title': 'Call Center Demo Agent',
            'metadata': {
                'user_id': persona.id,
                'role': persona.role,
                'is_demo': True,
                'pool': 'demo',
            },
        }
        try:
            resp = client.fabric.subscribers.create(**payload)
        except sw_client.SignalWireRestError as e:
            raise Exception(f"create subscriber failed: {e}")

        # The Fabric API wraps subscriber details under a `subscriber`
        # key. Top-level fields are the resource wrapper; we want both.
        subscriber_id = resp.get('id')
        nested = resp.get('subscriber') or {}
        if not subscriber_id:
            raise Exception(f"Subscriber response missing id: {resp}")

    # Pull the reference value the token API wants (email is what
    # SignalWire's create_token expects when no explicit username).
    reference = nested.get('email') or persona.email

    persona.signalwire_subscriber_id = subscriber_id
    persona.signalwire_username = reference
    persona.set_subscriber_password(password)
    persona.signalwire_address = f'/private/{fabric_address_name}'
    persona.fabric_subscriber_created_at = datetime.utcnow()
    db.session.commit()
    return mode
