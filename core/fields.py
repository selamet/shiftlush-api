"""Model fields with behaviour the schema depends on."""

from __future__ import annotations

from typing import Any

from django.db import models

from core.crypto import decrypt, encrypt


class EncryptedCharField(models.CharField):
    """Stores its value encrypted and hands back plaintext.

    Encryption happens at the boundary rather than in the callers, so nothing
    upstream has to remember to do it — a national ID written by a serializer,
    a management command or a test all land the same way.

    The column has to be wide enough for the ciphertext, which is why these
    fields are ``varchar(255)`` and not the eleven characters a TCKN occupies.
    A column sized for the plaintext is the clearest sign encryption was added
    as an afterthought.
    """

    def get_prep_value(self, value: Any) -> Any:
        if value is None:
            return None
        return encrypt(str(value))

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        if value is None:
            return None
        return decrypt(value)

    def to_python(self, value: Any) -> Any:
        if value is None:
            return None
        return decrypt(str(value))
