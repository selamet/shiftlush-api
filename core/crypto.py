"""Application-level encryption for the few columns that hold national IDs.

AES-256-GCM, which authenticates as well as encrypts: a tampered ciphertext
fails to decrypt rather than returning plausible garbage.

The stored form is ``v1:<base64(nonce || ciphertext || tag)>``. The version
prefix is there so the scheme can be rotated later without having to guess what
any given row was written with — a migration can read v1 and write v2.

What this deliberately does not provide is searchability. GCM uses a fresh
nonce per write, so the same national ID encrypts differently every time and
`WHERE national_id = ?` cannot work. That is the correct trade: a deterministic
scheme would let anyone with read access confirm whether a given person is in
the database. Equality, where it is genuinely needed, is answered by
``fingerprint()`` below and a column of its own, not by weakening the cipher.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PREFIX = "v1:"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class DecryptionError(RuntimeError):
    """Raised when a stored value cannot be decrypted."""


def _key() -> bytes:
    raw = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not raw:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set. Generate one with "
            "`python manage.py generate_encryption_key`."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is not valid base64.") from exc
    if len(key) != _KEY_BYTES:
        raise ImproperlyConfigured(
            f"FIELD_ENCRYPTION_KEY must decode to {_KEY_BYTES} bytes, got {len(key)}."
        )
    return key


def generate_key() -> str:
    """A fresh base64 key, for the management command and for tests."""
    return base64.urlsafe_b64encode(os.urandom(_KEY_BYTES)).decode()


def _fingerprint_key() -> bytes:
    """A separate key for the blind index, derived from the encryption key.

    Derived rather than configured so there is one secret to rotate, and
    separated rather than reused so that a fingerprint can never be mistaken for
    key material belonging to the cipher.
    """
    return hmac.new(_key(), b"shiftlush-blind-index-v1", hashlib.sha256).digest()


def fingerprint(value: str) -> str:
    """A keyed digest, for asking whether two encrypted values are the same.

    This is what makes a uniqueness constraint on an encrypted column possible.
    It is keyed rather than a bare SHA-256 on purpose: the space of national IDs
    is small enough to enumerate, so an unkeyed digest would be reversible by
    anyone who obtained the column.

    It still leaks equality — that is the entire point, and it is the reason
    this is used only where a duplicate has to be refused.
    """
    if value == "":
        # No value is not a value. Fingerprinting the empty string would make
        # every customer without a national ID collide with every other.
        return ""
    return hmac.new(_fingerprint_key(), value.encode(), hashlib.sha256).hexdigest()


def encrypt(plaintext: str) -> str:
    if plaintext == "":
        # An empty value stays empty: encrypting it would turn "not provided"
        # into a ciphertext that looks like data.
        return ""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_key()).encrypt(nonce, plaintext.encode(), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()


def decrypt(stored: str) -> str:
    if stored == "":
        return ""
    if not stored.startswith(PREFIX):
        # Plaintext left over from before the column was encrypted. Returning it
        # keeps the application working while a migration re-encrypts, instead
        # of failing every read of an old row.
        return stored
    blob = base64.urlsafe_b64decode(stored[len(PREFIX) :])
    nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(_key()).decrypt(nonce, sealed, None).decode()
    except InvalidTag as exc:
        raise DecryptionError(
            "Stored value failed authentication. Either the ciphertext was "
            "modified or FIELD_ENCRYPTION_KEY is not the key it was written with."
        ) from exc
