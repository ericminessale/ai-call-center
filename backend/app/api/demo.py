"""
Hosted-demo endpoints — public surface for the demo landing flow.

Two routes:
  - ``GET /api/config/runtime`` — public (no auth). Tells the frontend
    whether this instance is in DEMO_MODE and exposes the demo phone
    numbers to display on the landing card. Always available, returns
    ``demo_mode: false`` on a normal clone-and-own deployment.
  - ``POST /api/demo/start`` — only available when DEMO_MODE=true.
    Issues a JWT for a demo persona without requiring login. **M1 stub
    — returns the *same* demo user every time. M2 replaces this with
    the lease-from-pool implementation.**

When DEMO_MODE is off, ``/api/demo/start`` returns 404 — we don't even
hint that a demo path exists on a production-shape instance.

Rate-limit on ``/api/demo/start`` is intentional: a single client
shouldn't be able to mint a stream of demo JWTs by mashing the button.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app import db
from app.models import User
from app.utils.demo_config import (
    DEMO_AGENT_ROLE,
    is_demo_mode,
    runtime_config,
)
from app.utils.jwt_utils import generate_tokens

logger = logging.getLogger(__name__)

demo_bp = Blueprint('demo', __name__)


@demo_bp.route('/config/runtime', methods=['GET'])
def get_runtime_config():
    """Public runtime config the frontend consults on app boot.

    Unauthenticated by design — the frontend must be able to render the
    landing card before a session exists. Never include secrets here.
    """
    return jsonify(runtime_config())


@demo_bp.route('/demo/start', methods=['POST'])
def start_demo_session():
    """Mint a JWT for a demo persona and start the visitor's session.

    M1 behavior (stub): returns the first available demo persona, same
    one every time. Concurrent visitors all get the same identity —
    that's a known limitation that M2 (subscriber-pool leasing) fixes.
    Sufficient for landing-page wiring + UX validation.

    Refuses outright when DEMO_MODE is off so a production-shape
    deployment never accidentally exposes a no-auth login endpoint.
    """
    if not is_demo_mode():
        # Don't even confirm the route exists in production.
        return jsonify({'error': 'Not found'}), 404

    persona = (
        User.query.filter_by(role=DEMO_AGENT_ROLE, is_active=True)
        .order_by(User.id.asc())
        .first()
    )
    if persona is None:
        # Seed should have run on boot — log loudly if it didn't.
        logger.error(
            "demo/start: no demo personas in DB. Seed may have failed; "
            "check seed_demo_personas() startup logs."
        )
        return jsonify({
            'error': 'Demo not yet provisioned. Try again shortly.'
        }), 503

    tokens = generate_tokens(persona.id)
    logger.info(
        "demo/start: issued tokens for persona id=%s email=%s (M1 stub — "
        "shared across visitors until M2 ships the lease pool)",
        persona.id, persona.email,
    )
    return jsonify({
        'message': 'Demo session started',
        'user': persona.to_dict(),
        **tokens,
    }), 200
