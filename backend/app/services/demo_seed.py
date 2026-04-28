"""
Idempotent demo-persona seeding for the hosted demo deployment.

Runs at backend boot when ``DEMO_MODE=true``. Creates N (default 20)
``User`` rows with role ``demo_agent`` and persona names that look like
real teammates ("Alex Sales", "Jordan Support", etc.). These rows hold
the slots the M2 lease layer will hand out to anonymous visitors.

Idempotent on every dimension:
  - if the rows already exist, do nothing
  - if a partial set exists, top up to ``DEMO_POOL_SIZE``
  - never modifies or deletes existing rows (operator-edited names
    stay)

NOT a migration — pool size is configurable via env, and we don't want
prod clones running this seed accidentally. Migrations always run; this
runs only when the env flag says so.

The associated SignalWire Subscribers are NOT provisioned here — M2's
job. M1 leaves ``signalwire_subscriber_id`` null on these rows.
"""

from __future__ import annotations

import logging
import secrets

from app import db
from app.models import User
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
    """Top up the demo persona pool to the configured size. Idempotent.

    Returns a small dict for logging — caller logs at INFO level.
    """
    if not is_demo_mode():
        return {'skipped': 'DEMO_MODE not set'}

    target = demo_pool_size()
    existing_count = User.query.filter_by(role=DEMO_AGENT_ROLE).count()
    if existing_count >= target:
        return {
            'already_seeded': True,
            'existing': existing_count,
            'target': target,
        }

    # Walk slots 1..target; create any that are missing.
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
        # M2's lease flow issues JWTs directly via /api/demo/start.
        persona.set_password(secrets.token_urlsafe(32))
        db.session.add(persona)
        created += 1

    if created:
        db.session.commit()
    return {
        'created': created,
        'existing_before': existing_count,
        'target': target,
    }
