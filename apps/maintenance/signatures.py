"""The building's signature on a maintenance visit.

Spec: docs/superpowers/specs/2026-10-22-visit-signature-design.md. An
acknowledgement, like the paper service form it replaces — not a legal
e-signature. Every rule about it lives here, so the visit serializers and the
attachment service read one source rather than each keeping half.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from rest_framework import serializers

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.maintenance.models import MaintenanceVisit, SignatureStatus, VisitStatus
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError, Conflict

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


# The image ------------------------------------------------------------------


#: A drawn signature at 600x240 is a few kilobytes; this is generous headroom.
MAX_SIGNATURE_BYTES = 200 * 1024
SIGNATURE_MIME_TYPE = "image/png"


def _signed_visit_exists(company_id: uuid.UUID, visit_id: Any) -> bool:
    # Unscoped with an explicit company, as the attachment service reads its
    # targets: a permission-shaped check must not depend on company_context.
    return MaintenanceVisit.unscoped.filter(
        company_id=company_id, pk=visit_id, signature_status=SignatureStatus.SIGNED
    ).exists()


def _check_image(mime_type: str, size_bytes: int) -> None:
    if mime_type != SIGNATURE_MIME_TYPE:
        raise BusinessRuleError(ErrorCode.UNSUPPORTED_MIME_TYPE)
    if size_bytes > MAX_SIGNATURE_BYTES:
        raise BusinessRuleError(ErrorCode.FILE_TOO_LARGE)


def check_signature_upload(
    company_id: uuid.UUID, object_type: str, visit_id: Any, mime_type: str, size_bytes: int
) -> None:
    """Before a signature's upload URL is issued: a small PNG, for a signed visit."""
    if object_type != ObjectType.MAINTENANCE_VISIT:
        raise BusinessRuleError(ErrorCode.SIGNATURE_NOT_EXPECTED)
    _check_image(mime_type, size_bytes)
    if not _signed_visit_exists(company_id, visit_id):
        raise Conflict(ErrorCode.SIGNATURE_NOT_EXPECTED)


def check_signature_confirm(
    company_id: uuid.UUID, visit_id: Any, mime_type: str, size_bytes: int
) -> None:
    """Before a signature is recorded: still a signed visit, still its only one.

    A retried confirmation of the same object never reaches here — the
    attachment service returns the record it already made — so "exists" is
    always a second, different image.
    """
    _check_image(mime_type, size_bytes)
    if not _signed_visit_exists(company_id, visit_id):
        raise Conflict(ErrorCode.SIGNATURE_NOT_EXPECTED)
    if Attachment.unscoped.filter(
        company_id=company_id,
        object_type=ObjectType.MAINTENANCE_VISIT,
        object_id=visit_id,
        category=AttachmentCategory.SIGNATURE,
        is_deleted=False,
    ).exists():
        raise Conflict(ErrorCode.SIGNATURE_EXISTS)


# Reading ----------------------------------------------------------------------


class SignatureState:
    SIGNED = "signed"
    PENDING = "pending"
    NOT_PRESENT = "not_present"
    DECLINED = "declined"
    NONE = "none"

    ALL: tuple[str, ...] = (SIGNED, PENDING, NOT_PRESENT, DECLINED, NONE)


def _signature_images() -> QuerySet[Attachment]:
    return Attachment.unscoped.filter(
        object_type=ObjectType.MAINTENANCE_VISIT,
        object_id=OuterRef("pk"),
        category=AttachmentCategory.SIGNATURE,
        is_deleted=False,
    )


def with_signature(queryset: QuerySet[MaintenanceVisit]) -> QuerySet[MaintenanceVisit]:
    """Annotate whether each visit's signature image has arrived, in the list's query."""
    return queryset.annotate(has_signature_image=Exists(_signature_images()))


def signature_state(visit: MaintenanceVisit) -> str:
    """What every reader says about the signature — office, portal, report.

    "Pending" is a signed visit whose image has not arrived: the outbox sent
    the visit and is still sending the picture.
    """
    status = visit.signature_status
    if status == SignatureStatus.SIGNED:
        has_image = getattr(visit, "has_signature_image", None)
        if has_image is None:
            has_image = Attachment.unscoped.filter(
                object_type=ObjectType.MAINTENANCE_VISIT,
                object_id=visit.pk,
                category=AttachmentCategory.SIGNATURE,
                is_deleted=False,
            ).exists()
        return SignatureState.SIGNED if has_image else SignatureState.PENDING
    if status == SignatureStatus.NOT_PRESENT:
        return SignatureState.NOT_PRESENT
    if status == SignatureStatus.DECLINED:
        return SignatureState.DECLINED
    return SignatureState.NONE


def filter_by_state(queryset: QuerySet[MaintenanceVisit], state: str) -> QuerySet[MaintenanceVisit]:
    """Narrow an annotated queryset (`with_signature`) to one state."""
    signed = Q(signature_status=SignatureStatus.SIGNED)
    return {
        # Q, not keywords: the annotation is not a model field to the type checker.
        SignatureState.SIGNED: queryset.filter(signed & Q(has_signature_image=True)),
        SignatureState.PENDING: queryset.filter(signed & Q(has_signature_image=False)),
        SignatureState.NOT_PRESENT: queryset.filter(signature_status=SignatureStatus.NOT_PRESENT),
        SignatureState.DECLINED: queryset.filter(signature_status=SignatureStatus.DECLINED),
        SignatureState.NONE: queryset.filter(signature_status=""),
    }[state]
