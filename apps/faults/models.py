"""A lift fault, from the first report to the closed job.

M4: docs/superpowers/specs/2026-10-20-m4-fault-management-design.md. The state
and every timestamp are moved by the transition services in
apps.faults.services, never by a client, so "when did they get there" is a fact
the server recorded rather than a field somebody typed.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone

from core.models import CompanyOwnedModel


class FaultChannel(models.TextChoices):
    STAFF = "staff", "Staff"
    PUBLIC_QR = "public_qr", "Public QR report"
    CUSTOMER_PORTAL = "customer_portal", "Customer portal"


class FaultPriority(models.TextChoices):
    EMERGENCY = "emergency", "Emergency"
    HIGH = "high", "High"
    NORMAL = "normal", "Normal"


class FaultStatus(models.TextChoices):
    OPEN = "open", "Open"
    ASSIGNED = "assigned", "Assigned"
    ON_SITE = "on_site", "On site"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Cancelled"


#: The states a fault can still be cancelled from: everything before the work
#: is done. A resolved fault has a repair behind it, and undoing that is not a
#: cancellation.
CANCELLABLE = frozenset({FaultStatus.OPEN, FaultStatus.ASSIGNED, FaultStatus.ON_SITE})

# No 0/O, 1/I/L: a resident reads the code to somebody over the phone.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
REFERENCE_LENGTH = 6


def new_reference() -> str:
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


class Fault(CompanyOwnedModel):
    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="faults"
    )
    #: Six characters a person can read out — the only thing the public report
    #: answers with. Unique per firm, which is all a phone call needs.
    reference = models.CharField(max_length=REFERENCE_LENGTH)
    channel = models.CharField(max_length=20, choices=FaultChannel.choices)
    reported_by = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        related_name="reported_faults",
        null=True,
        blank=True,
    )
    #: The customer contact who reported it through the portal (M6). Identified,
    #: so neither the honeypot nor the merge of public reports applies.
    reported_by_portal_account = models.ForeignKey(
        "portal.PortalAccount",
        on_delete=models.PROTECT,
        related_name="reported_faults",
        null=True,
        blank=True,
    )
    # Personal data from a public report: blanked 90 days after the fault is
    # closed (apps.faults.tasks), and masked in the audit trail.
    reporter_name = models.CharField(max_length=100, blank=True)
    reporter_phone = models.CharField(max_length=30, blank=True)
    description = models.TextField(max_length=1000)
    #: Later public reports of the same fault, merged into it rather than made
    #: into faults of their own: [{reported_at, description, someone_trapped,
    #: elevator_stopped}]. No names or phone numbers — see services.report_public.
    additional_reports = models.JSONField(default=list, blank=True)
    priority = models.CharField(
        max_length=20, choices=FaultPriority.choices, default=FaultPriority.NORMAL
    )
    status = models.CharField(max_length=20, choices=FaultStatus.choices, default=FaultStatus.OPEN)
    technician = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        related_name="assigned_faults",
        null=True,
        blank=True,
    )
    reported_at = models.DateTimeField(default=timezone.now)
    assigned_at = models.DateTimeField(null=True, blank=True)
    on_site_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    resolution_notes = models.TextField(blank=True)
    parts_used = models.TextField(blank=True)
    # Null until resolve derives it (or the office states it first).
    billable = models.BooleanField(null=True, blank=True)
    billable_overridden = models.BooleanField(default=False)
    stopped_elevator = models.BooleanField(default=False)

    class Meta:
        db_table = "fault"
        ordering = ["-reported_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "reference"],
                condition=models.Q(is_deleted=False),
                name="uq_fault_reference_per_company",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=FaultStatus.values), name="fault_status_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(priority__in=FaultPriority.values),
                name="fault_priority_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=FaultChannel.values), name="fault_channel_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["technician", "status"]),
            models.Index(fields=["elevator", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} ({self.status})"

    @property
    def response_time(self) -> timedelta | None:
        """Reported to on site — what a firm promises a building."""
        return self.on_site_at - self.reported_at if self.on_site_at else None

    @property
    def resolution_time(self) -> timedelta | None:
        return self.resolved_at - self.reported_at if self.resolved_at else None
