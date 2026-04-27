"""
Symmetric encryption for at-rest secrets stored in the DB.

Wraps :mod:`cryptography.fernet` so callers don't have to think about key
loading or string/bytes plumbing. Key comes from
``SUBSCRIBER_PASSWORD_KEY`` (the name predates this module — it's reused
here so all encrypted columns share one key, and rotating the env var
rotates them together).

Usage::

    from app.utils.secrets_box import encrypt_secret, decrypt_secret

    blob = encrypt_secret("my-api-token")   # store in DB
    plain = decrypt_secret(blob)            # read back
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _load_key() -> bytes:
    """Return the active Fernet key, generating an ephemeral one in dev.

    Production must set ``SUBSCRIBER_PASSWORD_KEY``. If it's missing we
    log loudly and generate a one-shot key so nothing crashes during
    development; a restart will produce a different key, which is the
    point — production must opt out of that footgun by setting the env.
    """
    key = os.getenv('SUBSCRIBER_PASSWORD_KEY')
    if not key:
        key = Fernet.generate_key().decode()
        logger.warning(
            "SUBSCRIBER_PASSWORD_KEY not set — generated a one-shot key for "
            "this process. Set the env var to persist encrypted secrets "
            "across restarts. Generated key (do NOT use in prod): %s",
            key,
        )
    return key.encode() if isinstance(key, str) else key


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a string for at-rest storage. ``None`` → ``None``."""
    if plaintext is None or plaintext == '':
        return None
    fernet = Fernet(_load_key())
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: Optional[str]) -> Optional[str]:
    """Decrypt a previously-encrypted string. ``None`` → ``None``.

    Returns ``None`` on decryption failure (wrong key, corrupted blob)
    rather than raising, so callers can treat "couldn't decrypt" the
    same as "no value stored". Failures log at warning level.
    """
    if not ciphertext:
        return None
    try:
        fernet = Fernet(_load_key())
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.warning(
            "Failed to decrypt secret — likely SUBSCRIBER_PASSWORD_KEY rotated "
            "without re-encrypting stored values. Returning None."
        )
        return None
