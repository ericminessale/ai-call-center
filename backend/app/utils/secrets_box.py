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

import functools
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _load_key() -> bytes:
    """Return the active Fernet key, cached for the process.

    ``SUBSCRIBER_PASSWORD_KEY`` is REQUIRED. We fail fast when it's unset
    rather than generating an ephemeral key: a per-process / per-restart key
    silently makes previously-encrypted secrets undecryptable and can't be
    shared across gunicorn workers, so "encryption at rest" would provide no
    real durability or confidentiality. Caching also guarantees every
    encrypt/decrypt in this process uses the same key.
    """
    key = os.getenv('SUBSCRIBER_PASSWORD_KEY')
    if not key:
        raise RuntimeError(
            "SUBSCRIBER_PASSWORD_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" and set it in your .env. "
            "A stable key is required so encrypted secrets survive restarts "
            "and are shared across workers."
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
