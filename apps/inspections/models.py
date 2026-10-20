"""Periodic inspections, and the defects they find.

M5: docs/superpowers/specs/2026-10-20-m5-periodic-inspection-design.md. What a
label means — whether the lift may run, when its defects must be corrected —
is not here but in apps/inspections/rules.py, as data with its citations.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class Label(models.TextChoices):
    """The label an accredited inspection body leaves in the car."""

    GREEN = "green", "Green"
    BLUE = "blue", "Blue"
    YELLOW = "yellow", "Yellow"
    RED = "red", "Red"


class InspectionKind(models.TextChoices):
    PERIODIC = "periodic", "Periodic"
    FOLLOW_UP = "follow_up", "Follow-up"


class NonConformityStatus(models.TextChoices):
    OPEN = "open", "Open"
    FIXED = "fixed", "Fixed"
    WAIVED = "waived", "Waived"


class Inspection(CompanyOwnedModel):
    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="inspections"
    )
    kind = models.CharField(
        max_length=20, choices=InspectionKind.choices, default=InspectionKind.PERIODIC
    )
    #: The periodic inspection a follow-up follows. Its date is what a
    #: follow-up's deadline counts from: no inspection restarts the clock.
    follows = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="follow_ups", null=True, blank=True
    )
    inspected_on = models.DateField()
    # The names of the Phase 1 elevator fields they are copied into.
    inspection_body = models.CharField(max_length=150, blank=True)
    report_number = models.CharField(max_length=60, blank=True)
    label = models.CharField(max_length=10, choices=Label.choices)
    #: The report PDF, one of this inspection's own attachments
    #: (category `inspection_report`) — linked the way a contract's
    #: `signed_document` is, through attachments.services.link_attachment.
    report = models.ForeignKey(
        "attachments.Attachment",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    #: Defaults to a year on; editable, because the body sets the date.
    next_inspection_due = models.DateField()
    #: Derived from the label (rules.py) whenever the inspection is saved
    #: through the services. Stored, not computed on read, because the jobs and
    #: the "due" list filter and sort on it.
    correction_deadline = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "inspection"
        ordering = ["-inspected_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(label__in=Label.values), name="inspection_label_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=InspectionKind.values), name="inspection_kind_valid"
            ),
            # A follow-up without the inspection it follows has no date to
            # count its deadline from; a periodic one following something is
            # a contradiction.
            models.CheckConstraint(
                condition=(
                    models.Q(kind=InspectionKind.PERIODIC, follows__isnull=True)
                    | models.Q(kind=InspectionKind.FOLLOW_UP, follows__isnull=False)
                ),
                name="inspection_follow_up_has_origin",
            ),
            models.CheckConstraint(
                condition=models.Q(next_inspection_due__gt=models.F("inspected_on")),
                name="inspection_next_after_inspected",
            ),
        ]
        indexes = [
            models.Index(fields=["elevator", "-inspected_on"]),
            models.Index(fields=["company", "correction_deadline"]),
        ]

    def __str__(self) -> str:
        return f"{self.elevator_id} / {self.inspected_on} / {self.label}"


class NonConformity(CompanyOwnedModel):
    inspection = models.ForeignKey(
        Inspection, on_delete=models.PROTECT, related_name="nonconformities"
    )
    description = models.TextField()
    #: The label colour this defect caused.
    severity = models.CharField(max_length=10, choices=Label.choices)
    status = models.CharField(
        max_length=10, choices=NonConformityStatus.choices, default=NonConformityStatus.OPEN
    )
    fixed_on = models.DateField(null=True, blank=True)
    #: The M4 fault opened to fix it (services.open_fault).
    fault = models.ForeignKey(
        "faults.Fault",
        on_delete=models.PROTECT,
        related_name="nonconformities",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "inspection_nonconformity"
        ordering = ["inspection_id", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(severity__in=Label.values),
                name="nonconformity_severity_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=NonConformityStatus.values),
                name="nonconformity_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.inspection_id} / {self.status}"
