"""Managers that apply soft delete and tenant scoping by default."""

from __future__ import annotations

from typing import TypeVar

from django.db import models
from django.utils import timezone

from core.context import get_current_company_id, in_system_context

# Generic over the model so that `Elevator.objects.first()` is an Elevator and
# not Any. Left unparameterised, every read through these managers arrived
# untyped and carried that Any into whatever the caller returned.
ModelT = TypeVar("ModelT", bound=models.Model)


class SoftDeleteQuerySet(models.QuerySet[ModelT]):
    """A queryset whose ``delete()`` marks rows instead of removing them."""

    def delete(self) -> tuple[int, dict[str, int]]:
        # Django's QuerySet.delete() issues SQL directly and never calls the
        # model's delete(), so overriding the model alone would let a bulk
        # delete quietly destroy rows that are supposed to be recoverable.
        count = self.update(is_deleted=True, deleted_at=timezone.now())
        return count, {}

    def hard_delete(self) -> tuple[int, dict[str, int]]:
        """Remove rows for real. Management commands only."""
        return super().delete()

    def alive(self) -> SoftDeleteQuerySet[ModelT]:
        return self.filter(is_deleted=False)


class SoftDeleteManager(models.Manager[ModelT]):
    """Hides soft-deleted rows."""

    def get_queryset(self) -> SoftDeleteQuerySet[ModelT]:
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)


class TenantManager(SoftDeleteManager[ModelT]):
    """Adds the company filter on top of soft delete.

    This is the default manager, and it is also set as ``base_manager_name`` so
    that related access — ``building.elevator_set`` and friends — is filtered
    too. Without that, following a relation would step straight around the
    tenant boundary.
    """

    def get_queryset(self) -> SoftDeleteQuerySet[ModelT]:
        queryset = super().get_queryset()
        if in_system_context():
            return queryset
        company_id = get_current_company_id()
        if company_id is None:
            # Returning everything here would turn a missing context into a
            # cross-tenant read. An empty result is wrong too, but it is wrong
            # in the safe direction and shows up immediately in tests.
            return queryset.none()
        return queryset.filter(company_id=company_id)


class UnscopedManager(models.Manager[ModelT]):
    """No filters at all — neither tenant nor soft delete.

    Every row, including other companies' and deleted ones. That is what the
    name promises, and it is what the two callers need: audit tooling has to see
    across tenants, and anything reasoning about deletion has to see the rows
    that were deleted.

    Named to be conspicuous. ``Elevator.unscoped.all()`` should stop a reviewer
    mid-line; ``Elevator.all_objects.all()`` would read as ordinary.
    """

    def get_queryset(self) -> SoftDeleteQuerySet[ModelT]:
        return SoftDeleteQuerySet(self.model, using=self._db)
