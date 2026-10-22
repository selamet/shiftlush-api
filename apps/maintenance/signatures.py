"""The building's signature on a maintenance visit.

Spec: docs/superpowers/specs/2026-10-22-visit-signature-design.md. An
acknowledgement, like the paper service form it replaces — not a legal
e-signature. Every rule about it lives here, so the visit serializers and the
attachment service read one source rather than each keeping half.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework import serializers

from apps.maintenance.models import SignatureStatus, VisitStatus

#: Set once, on create. A signature is what the building agreed to then.
SIGNATURE_FIELDS: tuple[str, ...] = ("signature_status", "signer_name", "signer_role", "signed_at")


def _error(field: str, code: str, message: str) -> serializers.ValidationError:
    return serializers.ValidationError({field: serializers.ErrorDetail(message, code=code)})


def check_signature_fields(status: str, attrs: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    """Validate the signature fields of a visit being created; returns attrs.

    - A completed visit says whether the building signed, or why not. Any
      other visit may leave it blank: there may have been nobody to ask.
    - Signed needs a name and a role. "Nobody there" and "declined" carry
      neither — a name beside "declined" would read as somebody who signed.
    - `signed_at` is the device's clock, kept unless it is missing or ahead of
      the server's: a phone set to tomorrow cannot date a signature tomorrow.
    """
    if not creating:
        return attrs
    signature = attrs.get("signature_status", "")
    name = attrs.get("signer_name", "").strip()
    role = attrs.get("signer_role", "")

    if not signature:
        if status == VisitStatus.COMPLETED:
            raise _error("signature_status", "required", "This field is required.")
        if name or role:
            raise _error(
                "signer_name" if name else "signer_role",
                "invalid",
                "Only a signed visit names a signer.",
            )
        attrs.pop("signed_at", None)
        return attrs

    if signature == SignatureStatus.SIGNED:
        if not name:
            raise _error("signer_name", "required", "This field is required.")
        if not role:
            raise _error("signer_role", "required", "This field is required.")
        attrs["signer_name"] = name
        now = timezone.now()
        given = attrs.get("signed_at")
        attrs["signed_at"] = given if given is not None and given <= now else now
        return attrs

    if name:
        raise _error("signer_name", "invalid", "Only a signed visit names a signer.")
    if role:
        raise _error("signer_role", "invalid", "Only a signed visit names a signer.")
    attrs.pop("signed_at", None)
    return attrs
