from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class ContractStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    TERMINATED = "terminated", "Terminated"
    RENEWED = "renewed", "Renewed"


class Scope(models.TextChoices):
    MAINTENANCE_ONLY = "maintenance_only", "Maintenance only"
    MAINTENANCE_AND_REPAIR = "maintenance_and_repair", "Maintenance and repair"
    FULL_COVERAGE = "full_coverage", "Full coverage"


class PricingType(models.TextChoices):
    PER_ELEVATOR = "per_elevator", "Per elevator"
    FLAT = "flat", "Flat"


class BillingPeriod(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    SEMIANNUAL = "semiannual", "Semiannual"
    ANNUAL = "annual", "Annual"


class Contract(CompanyOwnedModel):
    """Attached to the customer, never to a building."""

    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="contracts"
    )
    contract_number = models.CharField(max_length=30)
    status = models.CharField(
        max_length=20, choices=ContractStatus.choices, default=ContractStatus.DRAFT
    )
    scope = models.CharField(max_length=32, choices=Scope.choices)

    start_date = models.DateField()
    end_date = models.DateField()

    pricing_type = models.CharField(max_length=20, choices=PricingType.choices)
    # Decimal, never float. Money that has been through a float is money you
    # cannot reconcile.
    monthly_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="TRY")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    billing_period = models.CharField(
        max_length=20, choices=BillingPeriod.choices, default=BillingPeriod.MONTHLY
    )

    auto_renew = models.BooleanField(default=False)
    renewal_notice_days = models.SmallIntegerField(default=60)
    previous_contract = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="renewals", null=True, blank=True
    )

    terminated_at = models.DateField(null=True, blank=True)
    termination_reason = models.TextField(blank=True)

    signed_document = models.ForeignKey(
        "attachments.Attachment",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "contract"
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "contract_number"],
                condition=models.Q(is_deleted=False),
                name="uq_contract_number_active",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=ContractStatus.values),
                name="contract_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="contract_end_after_start",
            ),
            # A terminated contract without a date is a record nobody can
            # reconcile later, so the database refuses it.
            models.CheckConstraint(
                condition=~models.Q(status=ContractStatus.TERMINATED)
                | models.Q(terminated_at__isnull=False),
                name="contract_terminated_requires_date",
            ),
        ]

    def __str__(self) -> str:
        return self.contract_number


class ContractElevator(CompanyOwnedModel):
    """One elevator's membership of one contract.

    When a contract ends the row is closed, not deleted: `removed_at` is filled
    so the billing history stays intact.
    """

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="lines")
    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="contract_lines"
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    added_at = models.DateField()
    removed_at = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "contract_elevator"
        ordering = ["-added_at"]
        constraints = [
            # An elevator can sit in only one open contract at a time, enforced
            # here rather than in application code.
            #
            # The is_deleted half is not optional: a soft-deleted line keeps
            # removed_at NULL, so without it the index would hold the elevator
            # hostage and the user would be told it is already under a contract
            # they cannot see anywhere.
            models.UniqueConstraint(
                fields=["elevator"],
                condition=models.Q(removed_at__isnull=True, is_deleted=False),
                name="uq_elevator_active_contract",
            ),
        ]
        indexes = [models.Index(fields=["contract", "removed_at"])]

    def __str__(self) -> str:
        return f"{self.contract_id} / {self.elevator_id}"
