"""The record a technician fills in for a routine monthly maintenance visit.

Not the periodic inspection module: Elevator.last_inspection_date and its
neighbours describe an independent third-party statutory inspection, a
different legal event from the company's own technician doing the monthly
visit this module covers. See the design doc for the full distinction.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class VisitStatus(models.TextChoices):
    COMPLETED = "completed", "Completed"
    INCOMPLETE = "incomplete", "Incomplete"
    ACCESS_DENIED = "access_denied", "Access denied"


class ChecklistItem(models.TextChoices):
    BRAKE_SYSTEM = "brake_system", "Brake system"
    HOISTING_ROPES = "hoisting_ropes", "Hoisting ropes and cables"
    LANDING_DOORS = "landing_doors", "Landing doors and locks"
    CAR_DOOR_SAFETY_EDGE = "car_door_safety_edge", "Car door and safety edge"
    LIMIT_SWITCHES = "limit_switches", "Limit switches"
    GUIDE_RAIL_LUBRICATION = "guide_rail_lubrication", "Guide rail lubrication"
    CAR_LIGHTING = "car_lighting", "Car lighting"
    EMERGENCY_LIGHTING = "emergency_lighting", "Emergency lighting"
    EMERGENCY_ALARM_INTERCOM = "emergency_alarm_intercom", "Emergency alarm / intercom"
    CONTROL_PANEL_BUTTONS = "control_panel_buttons", "Control panel and call buttons"
    LEVELING_ACCURACY = "leveling_accuracy", "Floor leveling accuracy"
    OVERLOAD_DEVICE = "overload_device", "Overload sensor"
    AUTOMATIC_RESCUE_DEVICE = "automatic_rescue_device", "Automatic rescue device (ARD) test"
    PIT_CONDITION = "pit_condition", "Pit condition and cleanliness"
    MACHINE_ROOM_CONDITION = "machine_room_condition", "Machine room condition and ventilation"
    EARTHING_GROUNDING = "earthing_grounding", "Earthing / grounding"


class ChecklistResult(models.TextChoices):
    OK = "ok", "OK"
    ATTENTION_NEEDED = "attention_needed", "Attention needed"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


class MaintenanceVisit(CompanyOwnedModel):
    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="maintenance_visits"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.PROTECT, related_name="maintenance_visits"
    )
    technician = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="maintenance_visits_performed"
    )
    visit_date = models.DateField()
    status = models.CharField(max_length=20, choices=VisitStatus.choices)
    status_note = models.TextField(blank=True)
    work_notes = models.TextField(blank=True)
    # Free text: nothing today queries which parts were used across the
    # fleet. A dedicated parts/inventory model is not built until something
    # does (YAGNI) - see the design doc.
    parts_replaced = models.TextField(blank=True)

    class Meta:
        db_table = "maintenance_visit"
        ordering = ["-visit_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=VisitStatus.values),
                name="maintenance_visit_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["elevator", "-visit_date"]),
            models.Index(fields=["company", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.elevator_id} / {self.visit_date}"


class MaintenanceVisitItem(CompanyOwnedModel):
    visit = models.ForeignKey(MaintenanceVisit, on_delete=models.PROTECT, related_name="items")
    item = models.CharField(max_length=32, choices=ChecklistItem.choices)
    result = models.CharField(max_length=20, choices=ChecklistResult.choices)
    note = models.TextField(blank=True)

    class Meta:
        db_table = "maintenance_visit_item"
        ordering = ["visit_id", "item", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["visit", "item"],
                condition=models.Q(is_deleted=False),
                name="uq_maintenance_visit_item",
            ),
            models.CheckConstraint(
                condition=models.Q(item__in=ChecklistItem.values),
                name="maintenance_visit_item_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(result__in=ChecklistResult.values),
                name="maintenance_visit_item_result_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.visit_id} / {self.item}"


class PlannedVisitStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    DONE = "done", "Done"
    CANCELLED = "cancelled", "Cancelled"


class PlannedVisit(CompanyOwnedModel):
    """One lift's maintenance visit for one month — the plan, not the record.

    M3: docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md.
    The daily `plan_maintenance` job creates these for this month and next; a
    recorded MaintenanceVisit closes one (apps.maintenance.services). Rows a
    person has edited are marked so the job never overwrites or cancels them.
    """

    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="planned_visits"
    )
    # "YYYY-MM". A string rather than the first of the month so the API filter
    # and the unique key read the way the screen says it: "Ekim 2026".
    period = models.CharField(max_length=7)
    due_date = models.DateField()
    planned_date = models.DateField(null=True, blank=True)
    technician = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        related_name="planned_visits",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20, choices=PlannedVisitStatus.choices, default=PlannedVisitStatus.PLANNED
    )
    # The visit that closed the row when `done`; while `planned`, the latest
    # attempt (incomplete, access denied), so the plan can say "tried, not done".
    visit = models.ForeignKey(
        MaintenanceVisit,
        on_delete=models.PROTECT,
        related_name="planned_visits",
        null=True,
        blank=True,
    )
    manually_edited = models.BooleanField(default=False)

    class Meta:
        db_table = "planned_visit"
        ordering = ["period", "due_date", "id"]
        constraints = [
            # A 30-day maximum interval makes one visit a month the obligation;
            # a second row for the same lift and month would be planned twice.
            models.UniqueConstraint(
                fields=["elevator", "period"],
                condition=models.Q(is_deleted=False),
                name="uq_planned_visit_per_month",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=PlannedVisitStatus.values),
                name="planned_visit_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "period", "status"]),
            models.Index(fields=["technician", "period"]),
        ]

    def __str__(self) -> str:
        return f"{self.elevator_id} / {self.period}"
