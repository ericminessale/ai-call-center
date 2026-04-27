"""Add mcp_gateway_configs table for customer-configurable external tools.

Each row is one MCP Gateway connection — a URL, credentials, optional
services allowlist, and the agent slugs that should load this gateway
at boot. The agents query this table via an internal endpoint and call
agent.add_skill('mcp_gateway', config) for each match.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-04-27 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mcp_gateway_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('gateway_url', sa.String(length=500), nullable=False),
        sa.Column('auth_type', sa.String(length=20), nullable=False, server_default='basic'),
        sa.Column('auth_user', sa.String(length=255), nullable=True),
        sa.Column('auth_password_encrypted', sa.Text(), nullable=True),
        sa.Column('auth_token_encrypted', sa.Text(), nullable=True),
        sa.Column('services_filter', sa.JSON(), nullable=True),
        sa.Column(
            'bound_agent_ids',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            'enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'ix_mcp_gateway_configs_enabled',
        'mcp_gateway_configs',
        ['enabled'],
    )


def downgrade():
    op.drop_index('ix_mcp_gateway_configs_enabled', table_name='mcp_gateway_configs')
    op.drop_table('mcp_gateway_configs')
