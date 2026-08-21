"""Abstract bases every business model derives from.

These three carry the decisions that are expensive to change later: the primary
key type, the delete semantics and the tenant boundary. Getting them wrong means
adding a column to forty tables afterwards, so they are written before any
concrete model.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone
from uuid_utils.compat import uuid7

from core.audit import AuditedModel
from core.context import get_current_company_id, in_system_context
from core.managers import SoftDeleteManager, TenantManager, UnscopedManager


class TenantMismatchError(RuntimeError):
    """Raised when a row is saved under a company other than the active one."""


class UUIDPrimaryKeyModel(AuditedModel):
    """UUIDv7 primary key.

    Time-ordered, so inserts stay at the right-hand edge of the B-tree instead
    of scattering across it the way uuid4 does — which matters once a table
    holds hundreds of thousands of rows. Sequential integers are not used at
    all: they would appear in URLs and leak record counts.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(UUIDPrimaryKeyModel):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        abstract = True


class SoftDeleteModel(TimeStampedModel):
    """Rows are marked deleted, never removed.

    An elevator's history has to outlive the elevator: an inspection record or a
    contract line that vanished would take the audit trail with it.

    Note for anyone adding a unique constraint to a subclass: it must carry
    ``condition=Q(is_deleted=False)``. Without it a deleted row keeps its
    business key forever, and the user is told a registration number is taken by
    a record they cannot see.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    unscoped = UnscopedManager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def delete(self, using: str | None = None, keep_parents: bool = False) -> Any:  # type: ignore[override]
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(using=using, update_fields=["is_deleted", "deleted_at", "updated_at"])
        return 1, {}

    def hard_delete(self, using: str | None = None) -> Any:
        """Remove the row for real. Management commands only."""
        return super().delete(using=using)


class CompanyOwnedModel(SoftDeleteModel):
    """Belongs to exactly one company.

    ``objects`` filters by the company in context, and is also the base manager
    so that relation traversal is filtered too.
    """

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
    )

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # A filtered read is only half the boundary: without this, a write could
        # still attach a row to someone else's company by setting the FK
        # directly. Reads and writes are checked separately on purpose.
        if not in_system_context():
            active = get_current_company_id()
            if active is not None and self.company_id is not None and self.company_id != active:
                raise TenantMismatchError(
                    f"{type(self).__name__} belongs to company {self.company_id} "
                    f"but the active company is {active}."
                )
        super().save(*args, **kwargs)


def default_uuid() -> uuid.UUID:
    """Exposed for models that need a UUID default outside the base classes."""
    return uuid7()


class IdempotencyKey(models.Model):
    """Replay protection for POST requests.

    Infrastructure rather than a business record, which is why it lives in core
    and is hard-deleted when it expires: there is nothing here worth preserving
    once the window closes.
    """

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=255)
    endpoint = models.CharField(max_length=200)
    # The same key with a different body is a client bug, not a retry. Replaying
    # the stored response there would return an answer to a question that was
    # never asked, so the hash lets that case be caught and rejected.
    request_hash = models.CharField(max_length=64)
    response_status = models.SmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "idempotency_key"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user", "key"], name="uq_idempotency_key_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"{self.endpoint} {self.key}"
