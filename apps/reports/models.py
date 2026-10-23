"""The monthly customer report: one stored record per customer and month.

Design: docs/superpowers/specs/2026-10-22-monthly-customer-report-design.md.
The PDF itself is an ordinary attachment; this row is what lists, the portal
and the office read without opening it.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class ReportDelivery(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    #: The firm's switch was off, so the report was stored and not e-mailed.
    NOT_ENABLED = "not_enabled", "Not enabled"
    #: Nobody on the customer is ticked to receive it.
    NO_RECIPIENT = "no_recipient", "No recipient"
    FAILED = "failed", "Failed"


class CustomerReport(CompanyOwnedModel):
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="reports"
    )
    #: "YYYY-MM", the Istanbul calendar month the report covers.
    period = models.CharField(max_length=7)
    file = models.ForeignKey(
        "attachments.Attachment",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    #: The summary numbers and the lead sentence's key, so a list can show them
    #: without opening the PDF.
    summary = models.JSONField(default=dict)
    delivery = models.CharField(
        max_length=20, choices=ReportDelivery.choices, default=ReportDelivery.PENDING
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    #: The addresses it went to, as they were then: a contact's e-mail can
    #: change afterwards and the record should still say where it was sent.
    recipients = models.JSONField(default=list)
    #: Why a delivery failed, for the office. Empty otherwise.
    error = models.TextField(blank=True)

    class Meta:
        db_table = "customer_report"
        ordering = ["-period", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "period"],
                condition=models.Q(is_deleted=False),
                name="uq_customer_report_period",
            ),
            models.CheckConstraint(
                condition=models.Q(delivery__in=ReportDelivery.values),
                name="customer_report_delivery_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.customer_id} {self.period}"
