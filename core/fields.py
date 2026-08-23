"""Model fields with behaviour the schema depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models

from core.crypto import decrypt, encrypt

if TYPE_CHECKING:
    # Django's fields carry type parameters in the stubs but are not
    # subscriptable at runtime — `CharField[str, str]` as a base class is a
    # TypeError on import. Split so the checker sees the parameters and the
    # interpreter sees the class. The alternative, django-stubs-ext's
    # monkeypatch, would put a typing package in the production dependencies.
    _CharFieldBase = models.CharField[str, str]
else:
    _CharFieldBase = models.CharField


class EncryptedCharField(_CharFieldBase):
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
