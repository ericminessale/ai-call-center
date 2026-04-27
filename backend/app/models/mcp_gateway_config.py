"""
McpGatewayConfig — admin-managed external tool integrations per agent.

The SignalWire SDK's ``mcp_gateway`` skill connects an agent to an MCP
*Gateway* service, which itself fronts one or more MCP (Model Context
Protocol) servers. Each row here is one gateway connection: a URL,
credentials, an optional service-name allowlist, and a list of agent
slugs that should load the skill at boot.

Per-call lifecycle isn't modeled here — that's the skill's job. We just
store config the AI agents pull at process startup.

Pitch: every CCaaS says "we integrate with Salesforce." That took them
18 months and breaks when Salesforce changes. We say: paste your gateway
URL. Your AI has whatever tools you exposed, in production, today.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app import db
from app.utils.secrets_box import decrypt_secret, encrypt_secret


# Auth modes accepted by the SDK skill (see signalwire.skills.mcp_gateway).
AUTH_TYPES = ('none', 'basic', 'bearer')


class McpGatewayConfig(db.Model):
    """One configured MCP Gateway connection."""

    __tablename__ = 'mcp_gateway_configs'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    gateway_url = db.Column(db.String(500), nullable=False)

    # auth_type drives which credentials we hand to the skill:
    #   none    → no auth on the gateway
    #   basic   → HTTP Basic with auth_user + auth_password (decrypted)
    #   bearer  → Authorization: Bearer <auth_token (decrypted)>
    auth_type = db.Column(db.String(20), nullable=False, default='basic')
    auth_user = db.Column(db.String(255), nullable=True)
    # Stored encrypted; decrypt with secrets_box. Never returned to the
    # frontend in plaintext.
    auth_password_encrypted = db.Column(db.Text, nullable=True)
    auth_token_encrypted = db.Column(db.Text, nullable=True)

    # Optional allowlist: which gateway-exposed services to surface to the
    # agent (empty / None = all). JSON list of either strings (service
    # names, all tools) or {"name": str, "tools": [str,...] | "*"} dicts —
    # this is exactly what the SDK skill's ``services`` param accepts.
    services_filter = db.Column(db.JSON, nullable=True)

    # Agent slugs (e.g. "sales-ai", "support-ai") that should load this
    # gateway at boot. Stored as JSON list — not a join table because the
    # set is small and rarely queried by agent.
    bound_agent_ids = db.Column(db.JSON, nullable=False, default=list)

    enabled = db.Column(db.Boolean, nullable=False, default=True, server_default='true')

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # ------------------------------------------------------------------
    # Credential plumbing
    # ------------------------------------------------------------------

    def set_auth_password(self, password: Optional[str]) -> None:
        self.auth_password_encrypted = encrypt_secret(password)

    def get_auth_password(self) -> Optional[str]:
        return decrypt_secret(self.auth_password_encrypted)

    def set_auth_token(self, token: Optional[str]) -> None:
        self.auth_token_encrypted = encrypt_secret(token)

    def get_auth_token(self) -> Optional[str]:
        return decrypt_secret(self.auth_token_encrypted)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Public-safe dict. Secrets only when explicitly requested.

        ``include_secrets=True`` is for the ai-agents internal endpoint
        where the agents need the cleartext credentials to talk to the
        gateway. Never expose to the admin frontend.
        """
        data: dict[str, Any] = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'gateway_url': self.gateway_url,
            'auth_type': self.auth_type,
            'auth_user': self.auth_user,
            'has_auth_password': bool(self.auth_password_encrypted),
            'has_auth_token': bool(self.auth_token_encrypted),
            'services_filter': self.services_filter or [],
            'bound_agent_ids': self.bound_agent_ids or [],
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_secrets:
            data['auth_password'] = self.get_auth_password()
            data['auth_token'] = self.get_auth_token()
        return data

    def to_skill_config(self) -> dict[str, Any]:
        """Build the kwargs dict the SDK's mcp_gateway skill expects.

        See signalwire.skills.mcp_gateway.skill.MCPGatewaySkill.setup
        for the contract. We only emit keys with meaningful values so
        the SDK falls back to its own defaults for unset options.
        """
        config: dict[str, Any] = {'gateway_url': self.gateway_url}
        if self.auth_type == 'basic':
            config['auth_user'] = self.auth_user or ''
            config['auth_password'] = self.get_auth_password() or ''
        elif self.auth_type == 'bearer':
            config['auth_token'] = self.get_auth_token() or ''
        if self.services_filter:
            config['services'] = self.services_filter
        return config

    def __repr__(self) -> str:
        return f"<McpGatewayConfig id={self.id} name={self.name!r} url={self.gateway_url!r}>"
