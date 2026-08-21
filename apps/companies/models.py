"""The tenant root."""

from __future__ import annotations

from django.db import models

from core.models import SoftDeleteModel


class Company(SoftDeleteModel):
    """A maintenance firm.

    Every other business table hangs off this one. Company itself is not
    tenant-scoped — it is the thing being scoped to — so it derives from
    SoftDeleteModel rather than CompanyOwnedModel.
    """

    legal_name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=80)

    tax_office = models.CharField(max_length=100, blank=True)
    # Ten digits for a company, eleven for a sole trader, so the column holds
    # both and the validator decides which rule applies.
    tax_number = models.CharField(max_length=11, blank=True)
    mersis_number = models.CharField(max_length=16, blank=True)
    trade_registry_number = models.CharField(max_length=30, blank=True)

    neighborhood = models.ForeignKey(
        "address.Neighborhood",
        on_delete=models.PROTECT,
        related_name="companies",
        null=True,
        blank=True,
    )
    street = models.CharField(max_length=150, blank=True)
    building_number = models.CharField(max_length=20, blank=True)
    unit_number = models.CharField(max_length=20, blank=True)

    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(max_length=150, blank=True)
    website = models.CharField(max_length=150, blank=True)

    # Nullable, and it has to be: this points at attachments.Attachment, which
    # points back here, so one side of the cycle must be optional for the
    # initial migration to be creatable at all.
    logo = models.ForeignKey(
        "attachments.Attachment",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "company"
        ordering = ["display_name"]
        verbose_name_plural = "companies"

    def __str__(self) -> str:
        return self.display_name
