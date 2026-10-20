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
