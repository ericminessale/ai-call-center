"""Seed an McpGatewayConfig row pointing at the bundled demo-mcp-gateway.

So that on first boot of a fresh clone, the External Tools tab already
has a working gateway visible — the cloner can hit Test, see the
DemoShop service + its tools, and (after restarting agents) place a call
that exercises real tool invocation. No external setup required.

The row is only inserted if no McpGatewayConfig rows exist yet, so this
migration is a no-op on environments that have already configured their
own gateways.

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-04-27 18:00:00.000000
"""
from __future__ import annotations

import json
import os

from alembic import op
import sqlalchemy as sa
from cryptography.fernet import Fernet


revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def _encrypt(plaintext: str) -> str:
    """Encrypt with the same Fernet key the runtime helper uses.

    Mirrors app.utils.secrets_box._load_key — falls back to a generated
    key if SUBSCRIBER_PASSWORD_KEY isn't set, but in that case the row
    won't decrypt at runtime. We accept that footgun here because the
    same env-var must be present for User.signalwire_password_encrypted
    to work, and that's already a hard requirement for the app.
    """
    key = os.getenv('SUBSCRIBER_PASSWORD_KEY')
    if not key:
        # No env key set — generate one so the migration succeeds, but
        # this row will be undecryptable at runtime. The app warns about
        # SUBSCRIBER_PASSWORD_KEY on its own; not our job to crash here.
        key = Fernet.generate_key().decode()
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key).encrypt(plaintext.encode()).decode()


def upgrade():
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM mcp_gateway_configs")).scalar()
    if existing and existing > 0:
        # Don't stomp on a real config — only seed empty environments.
        return

    user = os.getenv('DEMO_MCP_USER', 'demo')
    password = os.getenv('DEMO_MCP_PASSWORD', 'demo')

    bind.execute(
        sa.text(
            """INSERT INTO mcp_gateway_configs
                 (name, description, gateway_url, auth_type, auth_user,
                  auth_password_encrypted, services_filter, bound_agent_ids,
                  enabled, created_at, updated_at)
               VALUES
                 (:name, :description, :gateway_url, :auth_type, :auth_user,
                  :auth_password_encrypted, :services_filter, :bound_agent_ids,
                  :enabled, NOW(), NOW())"""
        ),
        {
            'name': 'DemoShop (bundled)',
            'description': (
                "Bundled demo MCP gateway with seeded customers, orders, products, "
                "and returns. Bound to the sales + support AI specialists by default. "
                "Replace with your own gateway when you're ready."
            ),
            'gateway_url': 'http://demo-mcp-gateway:8100',
            'auth_type': 'basic',
            'auth_user': user,
            'auth_password_encrypted': _encrypt(password),
            'services_filter': json.dumps([]),
            'bound_agent_ids': json.dumps(['sales-ai', 'support-ai']),
            'enabled': True,
        },
    )


def downgrade():
    op.execute(
        "DELETE FROM mcp_gateway_configs WHERE name = 'DemoShop (bundled)'"
    )
