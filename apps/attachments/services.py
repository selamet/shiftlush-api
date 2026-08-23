"""Uploading and reading files without the bytes touching this process.

Three rules shape everything here:

  - The client's claims about a file are not evidence. It asks for a URL by
    declaring a name, a type and a size; after the upload the record is written
    from what storage reports, not from what was declared.
  - The user's filename never becomes a key. Keys are generated, so a crafted
    name cannot decide where an object lands or what it overwrites.
  - A refused upload leaves nothing behind. If the object turns out to be too
    large or the wrong type it is deleted immediately, because the thirty-day
    sweeper only looks at rows, and an object with no row is invisible to it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound
from uuid_utils.compat import uuid7

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from core import storage
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError

#: Accepted types and the extension each one gets. Photographs of an installation
#: and scanned paperwork are the whole of phase 1; anything executable, archived
#: or scriptable is refused rather than filtered, because a blocklist of
#: dangerous types is a list nobody finishes.
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

MAX_SIZE_BYTES = 10 * 1024 * 1024

#: Which model each polymorphic `object_type` refers to. A plain map rather than
#: contenttypes: the join buys nothing and complicates the tenant filter.
TARGET_MODELS: dict[str, str] = {
    ObjectType.ELEVATOR: "elevators.Elevator",
    ObjectType.BUILDING: "properties.Building",
    ObjectType.CONTRACT: "contracts.Contract",
    ObjectType.CUSTOMER: "customers.Customer",
    ObjectType.COMPANY: "companies.Company",
    ObjectType.USER: "users.User",
}


@dataclass(frozen=True)
class UploadTicket:
    storage_key: str
    upload_url: str
    expires_in: int
    content_type: str


def _target_exists(company_id: uuid.UUID, object_type: str, object_id: uuid.UUID) -> bool:
    """Is there a live record of that type, with that id, in this company?

    Deliberately queried through `unscoped` with an explicit company filter
    rather than through the ambient tenant context: this runs from management
    commands too, and a check that silently returns nothing outside a request is
    worse than no check.
    """
    model = apps.get_model(TARGET_MODELS[object_type])
    queryset = model.unscoped.filter(pk=object_id, is_deleted=False)
    if object_type == ObjectType.COMPANY:
        # Company is the tenant; it has no column pointing at itself.
        return bool(queryset.filter(pk=company_id).exists())
    return bool(queryset.filter(company_id=company_id).exists())


def _backend_for(category: str) -> str:
    """Where a new file of this category goes.

    The override map is empty today. It exists so that moving the categories
    holding personal data to a Turkey-resident provider is a settings change:
    new files follow the map, old rows keep the backend recorded on them.
    """
    return str(settings.STORAGE_BACKEND_BY_CATEGORY.get(category, settings.DEFAULT_STORAGE_BACKEND))


def prepare_upload(
    *,
    company_id: uuid.UUID,
    object_type: str,
    object_id: uuid.UUID,
    category: str,
    mime_type: str,
    size_bytes: int,
) -> UploadTicket:
    """Check what can be checked before the bytes exist, then hand out a URL.

    The size here is a promise, not a fact — nothing stops a client from
    uploading more. Refusing early is still worth it: it saves a doomed 40 MB
    upload over a mobile connection, and `confirm_upload` measures the real
    thing afterwards.
    """
    if mime_type not in ALLOWED_TYPES:
        raise BusinessRuleError(ErrorCode.UNSUPPORTED_MIME_TYPE)
    if size_bytes <= 0 or size_bytes > MAX_SIZE_BYTES:
        raise BusinessRuleError(ErrorCode.FILE_TOO_LARGE)
    if not _target_exists(company_id, object_type, object_id):
        # 404 rather than a validation error: answering "that elevator is not
        # yours" differently from "that elevator does not exist" turns this into
        # a way to count another company's records.
        raise NotFound()

    # Everything the confirmation needs is in the key. That is not decoration:
    # it means the second call cannot disagree with the first about what was
    # uploaded or where it went. A client that asked for a signed URL under one
    # category and confirmed under another would otherwise have the server
    # looking in the wrong bucket.
    #
    # The company id leads so a key from one tenant cannot be confirmed by
    # another, and the random part is a UUIDv7, which sorts by time and keeps a
    # bucket listing readable.
    key = f"{company_id}/{object_type}/{object_id}/{category}/{uuid7()}{ALLOWED_TYPES[mime_type]}"
    backend = _backend_for(category)
    return UploadTicket(
        storage_key=key,
        upload_url=storage.upload_url(backend, key, mime_type),
        expires_in=settings.UPLOAD_URL_TTL_SECONDS,
        content_type=mime_type,
    )


def _parse_key(company_id: uuid.UUID, storage_key: str) -> tuple[str, uuid.UUID, str]:
    """Read back what `prepare_upload` wrote into the key.

    A forged key gets no further than this, and even a well-formed forgery is
    useless: the only keys with an object behind them are the ones this server
    signed, so `stat` refuses the rest a few lines later.
    """
    parts = storage_key.split("/")
    if len(parts) != 5 or parts[0] != str(company_id):
        raise NotFound()

    _, object_type, raw_object_id, category, _filename = parts
    if object_type not in TARGET_MODELS or category not in AttachmentCategory.values:
        raise NotFound()
    try:
        object_id = uuid.UUID(raw_object_id)
    except ValueError as exc:
        raise NotFound() from exc
    return object_type, object_id, category


@transaction.atomic
def confirm_upload(
    *,
    company_id: uuid.UUID,
    uploaded_by: Any,
    storage_key: str,
    original_filename: str,
) -> Attachment:
    """Record a file that is already in the bucket.

    Size and type come from storage, so a client that asked for permission to
    upload a 2 MB JPEG and then uploaded a 9 MB one is recorded honestly — and
    one that uploaded 40 MB has it deleted here rather than kept forever.
    """
    object_type, object_id, category = _parse_key(company_id, storage_key)

    existing = Attachment.unscoped.filter(
        company_id=company_id, storage_key=storage_key, is_deleted=False
    ).first()
    if existing is not None:
        # A retried confirmation is the same confirmation. Creating a second row
        # for one object would give the sweeper two owners for the same bytes.
        return existing

    if not _target_exists(company_id, object_type, object_id):
        raise NotFound()

    backend = _backend_for(category)
    try:
        stored = storage.stat(backend, storage_key)
    except storage.ObjectNotFound as exc:
        # The signed URL was requested but the upload never landed. Telling the
        # client this plainly is what lets it retry the PUT instead of the POST.
        raise BusinessRuleError(ErrorCode.UPLOAD_NOT_COMPLETED) from exc

    if stored.size_bytes > MAX_SIZE_BYTES:
        storage.delete(backend, storage_key)
        raise BusinessRuleError(ErrorCode.FILE_TOO_LARGE)
    if stored.content_type not in ALLOWED_TYPES:
        storage.delete(backend, storage_key)
        raise BusinessRuleError(ErrorCode.UNSUPPORTED_MIME_TYPE)

    return Attachment.objects.create(
        company_id=company_id,
        object_type=object_type,
        object_id=object_id,
        category=category,
        original_filename=original_filename[:255],
        mime_type=stored.content_type,
        size_bytes=stored.size_bytes,
        storage_key=storage_key,
        storage_backend=backend,
        uploaded_by=uploaded_by,
    )


def download_url(attachment: Attachment) -> str:
    """A short-lived URL for one file, signed against its own backend."""
    if not attachment.storage_key:
        # Soft-deleted long enough ago that the sweeper removed the object. The
        # row survives because the audit trail refers to it.
        raise BusinessRuleError(ErrorCode.UPLOAD_NOT_COMPLETED)
    return storage.download_url(
        attachment.storage_backend,
        attachment.storage_key,
        attachment.original_filename,
        attachment.mime_type,
    )


def link_attachment(record: models.Model, attachment: Attachment, field: str) -> None:
    """Point a record at one of its own attachments.

    `company.logo` and `contract.signed_document` are foreign keys on top of the
    polymorphic relation, and they answer a different question: not "this file
    belongs to that record" but "this is the record's *current* one". A contract
    can hold several signed PDFs — the original and an addendum — and the key
    marks which one is in force.

    Both directions are checked here. A foreign key pointing at another
    company's file would show one firm another firm's documents, which is a
    tenant leak wearing the costume of a convenience field.
    """
    if attachment.company_id != getattr(record, "company_id", record.pk):
        raise BusinessRuleError(ErrorCode.ATTACHMENT_TARGET_MISMATCH)
    if str(attachment.object_id) != str(record.pk):
        raise BusinessRuleError(ErrorCode.ATTACHMENT_TARGET_MISMATCH)
    setattr(record, field, attachment)
    record.save(update_fields=[field, "updated_at"])


def _detached(older_than_days: int | None) -> models.QuerySet[Attachment]:
    """Attachments whose bytes are due for removal.

    Read through `unscoped` on purpose: this crosses every company, and a
    company-scoped manager outside a request would quietly return nothing and
    make the sweeper look like it had finished its work.
    """
    days = older_than_days if older_than_days is not None else settings.ATTACHMENT_PURGE_AFTER_DAYS
    cutoff = timezone.now() - timedelta(days=days)
    return Attachment.unscoped.filter(is_deleted=True, deleted_at__lte=cutoff, storage_key__gt="")


def count_detached_objects(older_than_days: int | None = None) -> int:
    return int(_detached(older_than_days).count())


def purge_detached_objects(older_than_days: int | None = None) -> int:
    """Delete the bytes of attachments soft-deleted long enough ago.

    The row is kept and only `storage_key` is cleared: an audit entry saying a
    file was removed is worth nothing if the record of what was removed goes
    with it.
    """
    stale = _detached(older_than_days).only("id", "storage_key", "storage_backend")

    purged = 0
    for attachment in stale.iterator():
        storage.delete(attachment.storage_backend, attachment.storage_key)
        # Cleared one row at a time rather than in a bulk update: a crash halfway
        # through must not leave rows claiming their bytes are gone when they are
        # still there, or the reverse.
        Attachment.unscoped.filter(pk=attachment.pk).update(storage_key="")
        purged += 1
    return purged
