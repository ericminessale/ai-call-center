from datetime import datetime
from app import db, bcrypt
from cryptography.fernet import Fernet
import os
import base64


class User(db.Model):
    """User model for authentication."""

    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(50), default='agent', nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Call Fabric Subscriber Info
    signalwire_subscriber_id = db.Column(db.String(100), unique=True, nullable=True, index=True)
    signalwire_username = db.Column(db.String(100), unique=True, nullable=True, index=True)
    signalwire_password_encrypted = db.Column(db.String(500), nullable=True)
    signalwire_address = db.Column(db.String(255), nullable=True)
    fabric_subscriber_created_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    calls = db.relationship('Call', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<User {self.email}>'

    def set_password(self, password):
        """Hash and set the user's password."""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """Check if the provided password matches the hash."""
        return bcrypt.check_password_hash(self.password_hash, password)

    @staticmethod
    def _get_encryption_key():
        """Get or generate encryption key for subscriber passwords."""
        key = os.getenv('SUBSCRIBER_PASSWORD_KEY')
        if not key:
            # Generate a key if not set (for development)
            # In production, this MUST be set in environment
            key = Fernet.generate_key().decode()
            print(f"WARNING: Generated temporary encryption key. Set SUBSCRIBER_PASSWORD_KEY={key}")

        # Ensure key is properly formatted
        if isinstance(key, str):
            key = key.encode()
        return key

    def set_subscriber_password(self, password):
        """Encrypt and store subscriber password."""
        if not password:
            self.signalwire_password_encrypted = None
            return

        key = self._get_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(password.encode())
        self.signalwire_password_encrypted = encrypted.decode()

    def get_subscriber_password(self):
        """Decrypt and return subscriber password."""
        if not self.signalwire_password_encrypted:
            return None

        key = self._get_encryption_key()
        fernet = Fernet(key)
        decrypted = fernet.decrypt(self.signalwire_password_encrypted.encode())
        return decrypted.decode()

    def to_dict(self):
        """Convert user to dictionary."""
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'role': self.role,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'has_subscriber': self.signalwire_subscriber_id is not None,
            'signalwire_address': self.signalwire_address
        }

    @classmethod
    def find_by_email(cls, email):
        """Find user by email."""
        return db.session.query(cls).filter_by(email=email).first()

    @classmethod
    def find_by_id(cls, user_id):
        """Find user by ID."""
        return db.session.query(cls).filter_by(id=user_id).first()