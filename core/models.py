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

    def delete(self, using: str | None = None, keep_parents: bool = False) -> Any:
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

    # django-stubs marks a model's managers as class variables, and does not
    # recognise an abstract base replacing one it declared itself. There is no
    # narrower code to silence and no way to declare it that keeps the manager
    # parameterised per concrete model, which is what makes reads typed at all.
    objects = TenantManager()  # type: ignore[misc]
    unscoped = UnscopedManager()  # type: ignore[misc]

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
    # Whose key it is: a staff user, or (M6) a customer portal account. Exactly
    # one — two principals sharing a key space would let one replay the other's
    # answer.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", null=True, blank=True
    )
    portal_account = models.ForeignKey(
        "portal.PortalAccount", on_delete=models.CASCADE, related_name="+", null=True, blank=True
    )
    key = models.CharField(max_length=255)
    endpoint = models.CharField(max_length=200)
    # The same key with a different body is a client bug, not a retry. Replaying
    # the stored response there would return an answer to a question that was
    # never asked, so the hash lets that case be caught and rejected.
    request_hash = models.CharField(max_length=64)
    # Both empty while the key is reserved and its handler is still running
    # (issue #155): the row is written *before* the work, so a retry that
    # overlaps the first request finds it and is refused instead of running the
    # handler a second time. Both are filled once the handler succeeds; a
    # failed handler deletes the row instead.
    response_status = models.SmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    # For a pending row, when the current reservation was taken: an abandoned
    # reservation is taken over by moving it forward (see core/idempotency.py).
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "idempotency_key"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user", "key"],
                condition=models.Q(user__isnull=False),
                name="uq_idempotency_key_per_user",
            ),
            models.UniqueConstraint(
                fields=["company", "portal_account", "key"],
                condition=models.Q(portal_account__isnull=False),
                name="uq_idempotency_key_per_portal_account",
            ),
            models.CheckConstraint(
                condition=models.Q(user__isnull=False, portal_account__isnull=True)
                | models.Q(user__isnull=True, portal_account__isnull=False),
                name="idempotency_key_one_principal",
            ),
            # A pending row has no answer yet. The reverse is not required: a
            # stored 2xx whose body is empty is a legitimate answer, and None
            # reaches a nullable JSONField as SQL NULL.
            models.CheckConstraint(
                condition=models.Q(response_status__isnull=False)
                | models.Q(response_body__isnull=True),
                name="idempotency_key_pending_has_no_body",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.endpoint} {self.key}"

    @property
    def is_pending(self) -> bool:
        return self.response_status is None


class ScheduledRun(models.Model):
    """One run of one scheduled job, claimed before its task is enqueued.

    The unique (job, period) is the whole of the scheduler's idempotency: see
    core/scheduling.py. Infrastructure, like IdempotencyKey, so no tenant and
    no soft delete.
    """

    id = models.BigAutoField(primary_key=True)
    job = models.CharField(max_length=100)
    # The local date for a daily job, e.g. "2026-10-20". A string so a monthly
    # schedule can use "2026-10" without a schema change.
    period = models.CharField(max_length=20)
    task_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scheduled_run"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["job", "period"], name="uq_scheduled_run_per_period")
        ]

    def __str__(self) -> str:
        return f"{self.job} {self.period}"
