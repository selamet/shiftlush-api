"""Invoice drafts: what the firm will bill a contract for one period.

M7: docs/superpowers/specs/2026-10-21-m7-billing-drafts-design.md. A draft is
not an invoice. A Turkish invoice is issued through e-Fatura or e-Arsiv by the
firm's own integrator; the accountant copies the draft there and records the
invoice number and date back here.

Money is Decimal throughout, rounded half-up to two places at the points the
spec names (apps.billing.money), never float.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ISSUED = "issued", "Issued"
    PAID = "paid", "Paid"
    CANCELLED = "cancelled", "Cancelled"


class InvoiceLineKind(models.TextChoices):
    CONTRACT_FEE = "contract_fee", "Contract fee"
    ELEVATOR_FEE = "elevator_fee", "Elevator fee"
    FAULT = "fault", "Fault"
    MANUAL = "manual", "Manual"


class InvoiceDraft(CompanyOwnedModel):
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.PROTECT, related_name="invoice_drafts"
    )
    #: Copied from the contract, so the list filters by customer without a join
    #: and a draft keeps naming whom it billed.
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="invoice_drafts"
    )
    #: Inclusive at both ends.
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(
        max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT
    )
    currency = models.CharField(max_length=3)
    #: Copied when the draft is generated: a rate changed on the contract later
    #: must not rewrite what a past period was billed at.
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    # Derived from the lines (apps.billing.services.recompute) on every change
    # while in draft, and frozen from issue on.
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    invoice_number = models.CharField(max_length=40, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "invoice_draft"
        ordering = ["-period_start", "-id"]
        constraints = [
            # One live draft per contract per period: the generator's
            # idempotency. A cancelled draft gives the period back, so the next
            # run drafts it again — with the faults the cancellation released.
            models.UniqueConstraint(
                fields=["contract", "period_start"],
                condition=models.Q(is_deleted=False) & ~models.Q(status="cancelled"),
                name="uq_invoice_draft_per_period",
            ),
            # An invoice number names one invoice in the firm's books, so two
            # issued drafts may not share it. Cancelled ones release it.
            models.UniqueConstraint(
                fields=["company", "invoice_number"],
                condition=models.Q(status__in=["issued", "paid"], is_deleted=False),
                name="uq_invoice_number_per_company",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=InvoiceStatus.values), name="invoice_status_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="invoice_period_ordered",
            ),
            # Issued means the accountant recorded where it was issued.
            models.CheckConstraint(
                condition=~models.Q(status__in=["issued", "paid"])
                | (~models.Q(invoice_number="") & models.Q(invoice_date__isnull=False)),
                name="invoice_issued_has_number_and_date",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="paid") | models.Q(paid_on__isnull=False),
                name="invoice_paid_has_date",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "period_start"]),
        ]

    def __str__(self) -> str:
        return f"{self.contract_id} {self.period_start}..{self.period_end} ({self.status})"


class InvoiceLine(CompanyOwnedModel):
    draft = models.ForeignKey(InvoiceDraft, on_delete=models.PROTECT, related_name="lines")
    kind = models.CharField(max_length=20, choices=InvoiceLineKind.choices)
    description = models.CharField(max_length=500)
    elevator = models.ForeignKey(
        "elevators.Elevator",
        on_delete=models.PROTECT,
        related_name="invoice_lines",
        null=True,
        blank=True,
    )
    fault = models.ForeignKey(
        "faults.Fault",
        on_delete=models.PROTECT,
        related_name="invoice_lines",
        null=True,
        blank=True,
    )
    #: Set when the draft is cancelled. The line keeps naming its fault for the
    #: record; the fault itself is free to be drafted again.
    fault_released = models.BooleanField(default=False)
    #: Months, possibly prorated by covered days, to four places; or a count.
    #: For the reader: a prorated line's amount is not computed from this
    #: rounded figure but from the exact fraction below.
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    # The exact fraction a prorated fee line bills: `months x covered_days /
    # period_days` of the monthly price. Null on every other line, whose
    # quantity is exact as it stands. Kept so that pricing such a line later
    # still charges the exact share, not the rounded quantity's.
    months = models.PositiveSmallIntegerField(null=True, blank=True)
    covered_days = models.PositiveSmallIntegerField(null=True, blank=True)
    period_days = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Null until known: a fault carries no price, and a per-elevator line may
    #: have none on the contract.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    #: quantity x unit_price, rounded half-up to two places; zero while unpriced.
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    needs_price = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "invoice_line"
        ordering = ["draft_id", "position", "id"]
        constraints = [
            # A fault appears on one live draft only. Enforced here rather than
            # by a lookup before insert, so two runs racing cannot both bill it;
            # a cancelled draft's lines are released and soft-deleted lines do
            # not hold it either.
            models.UniqueConstraint(
                fields=["fault"],
                condition=models.Q(fault__isnull=False, fault_released=False, is_deleted=False),
                name="uq_invoice_line_fault_billed_once",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=InvoiceLineKind.values), name="invoice_line_kind_valid"
            ),
            models.CheckConstraint(
                condition=~models.Q(kind="fault") | models.Q(fault__isnull=False),
                name="invoice_line_fault_names_fault",
            ),
            # All three or none: half a fraction bills nothing anyone can check.
            models.CheckConstraint(
                condition=models.Q(
                    months__isnull=True, covered_days__isnull=True, period_days__isnull=True
                )
                | models.Q(
                    months__isnull=False,
                    covered_days__isnull=False,
                    period_days__gt=0,
                ),
                name="invoice_line_proration_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.draft_id} / {self.kind} / {self.amount}"
