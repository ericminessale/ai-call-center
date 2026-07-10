from datetime import datetime

from app import db


class SubscriberSeat(db.Model):
    """A pre-provisioned SignalWire subscriber, decoupled from User rows.

    Subscribers are the one expensive platform resource, so they form a
    fixed pool sized to max *concurrently online* browsers — not max
    workspaces. In TENANCY_MODE a visitor's browser leases a seat when it
    needs WebRTC (``POST /api/fabric/token``) via the Redis SETNX lease in
    :mod:`app.services.seat_lease`; the pool is topped up / adopted at boot
    by :mod:`app.services.seat_pool` (migration u1v2w3x4y5z6 copies the old
    demo personas' subscriber credentials in).

    Clone-and-own deployments never touch seats — real users keep their
    per-user subscriber columns on ``users`` and the original
    ``fabric.py`` auto-provision path.

    ``email`` is the SignalWire-side adoption identity: seat provisioning
    looks up existing subscribers by email before creating, so rebuilt
    databases re-link the space's existing subscribers instead of minting
    duplicates. Global (NOT WorkspaceScoped) by design.
    """

    __tablename__ = 'subscriber_seats'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(255), nullable=True)

    signalwire_subscriber_id = db.Column(db.String(100), unique=True, nullable=True, index=True)
    signalwire_username = db.Column(db.String(100), nullable=True)
    signalwire_password_encrypted = db.Column(db.String(500), nullable=True)
    # The REAL platform-resolved fabric address (``/private/<slug>``) —
    # never the old fabricated ``/private/agent-<id>`` form. seat_pool
    # re-resolves any seat still carrying a fabricated/missing address.
    signalwire_address = db.Column(db.String(255), nullable=True)
    provisioned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Informational mirror of the authoritative Redis lease (seat_lease.py).
    leased_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
    )
    leased_at = db.Column(db.DateTime, nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)

    def set_subscriber_password(self, password):
        from app.utils.secrets_box import encrypt_secret
        self.signalwire_password_encrypted = encrypt_secret(password or None)

    def get_subscriber_password(self):
        from app.utils.secrets_box import decrypt_secret
        return decrypt_secret(self.signalwire_password_encrypted)

    def has_credentials(self) -> bool:
        return bool(
            self.signalwire_subscriber_id
            and self.signalwire_username
            and self.signalwire_password_encrypted
        )

    def address_needs_resolution(self) -> bool:
        """True when the address is missing or still the fabricated form."""
        if not self.signalwire_subscriber_id:
            return False
        addr = self.signalwire_address or ''
        return not addr or addr.startswith('/private/agent-')

    def __repr__(self):
        return f'<SubscriberSeat {self.id} {self.email}>'
