"""The central record.

Most fields are optional but all of them exist from day one: adding a column
later means a migration plus back-filling data nobody has anymore. Phase 1 has
no inspection module, yet the inspection fields are here so the module can be
built on top of records that already carry the data.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class Category(models.TextChoices):
    PASSENGER = "passenger", "Passenger"
    FREIGHT = "freight", "Freight"
    PASSENGER_FREIGHT = "passenger_freight", "Passenger and freight"
    DUMBWAITER = "dumbwaiter", "Dumbwaiter"
    ACCESSIBILITY_PLATFORM = "accessibility_platform", "Accessibility platform"
    VEHICLE = "vehicle", "Vehicle"


class DriveType(models.TextChoices):
    GEARED_ELECTRIC = "geared_electric", "Geared electric"
    GEARLESS_ELECTRIC = "gearless_electric", "Gearless electric"
    HYDRAULIC = "hydraulic", "Hydraulic"


class ControlType(models.TextChoices):
    SIMPLE_COLLECTIVE = "simple_collective", "Simple collective"
    DOWN_COLLECTIVE = "down_collective", "Down collective"
    FULL_COLLECTIVE = "full_collective", "Full collective"
    GROUP_CONTROL = "group_control", "Group control"


class DoorType(models.TextChoices):
    AUTOMATIC_CENTER = "automatic_center", "Automatic centre opening"
    AUTOMATIC_SIDE = "automatic_side", "Automatic side opening"
    SEMI_AUTOMATIC = "semi_automatic", "Semi automatic"
    MANUAL = "manual", "Manual"


class MachineRoom(models.TextChoices):
    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    PARTIAL = "partial", "Partial"


class InspectionLabel(models.TextChoices):
    GREEN = "green", "Green"
    BLUE = "blue", "Blue"
    YELLOW = "yellow", "Yellow"
    RED = "red", "Red"
    NONE = "none", "None"


class ElevatorStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    SEALED = "sealed", "Sealed"
    OUT_OF_SERVICE = "out_of_service", "Out of service"
    # Derived from whether an open contract line exists, but stored so lists can
    # filter on it. Only the contract service writes it; the serializer rejects
    # it as user input.
    UNCONTRACTED = "uncontracted", "Uncontracted"


USER_SELECTABLE_STATUSES = [
    ElevatorStatus.ACTIVE,
    ElevatorStatus.SUSPENDED,
    ElevatorStatus.SEALED,
    ElevatorStatus.OUT_OF_SERVICE,
]


class Elevator(CompanyOwnedModel):
    building = models.ForeignKey(
        "properties.Building", on_delete=models.PROTECT, related_name="elevators"
    )

    # Identity ---------------------------------------------------------------
    registration_number = models.CharField(max_length=30, blank=True)
    internal_code = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=100, blank=True)
    # 12 random characters, never derived from the id or the registration
    # number: a guessable token lets a competitor walk the whole estate.
    qr_token = models.CharField(max_length=24, unique=True)
    qr_token_generated_at = models.DateTimeField(null=True, blank=True)

    # Classification ---------------------------------------------------------
    category = models.CharField(max_length=32, choices=Category.choices, blank=True)
    drive_type = models.CharField(max_length=32, choices=DriveType.choices, blank=True)
    control_type = models.CharField(max_length=32, choices=ControlType.choices, blank=True)
    door_type = models.CharField(max_length=32, choices=DoorType.choices, blank=True)
    # A missing car door is a serious non-conformity at inspection, so it has to
    # be reportable rather than buried in free text.
    has_car_door = models.BooleanField(null=True, blank=True)
    machine_room = models.CharField(max_length=16, choices=MachineRoom.choices, blank=True)

    # Technical --------------------------------------------------------------
    capacity_kg = models.IntegerField(null=True, blank=True)
    capacity_persons = models.SmallIntegerField(null=True, blank=True)
    stop_count = models.SmallIntegerField(null=True, blank=True)
    entrance_count = models.SmallIntegerField(null=True, blank=True)
    speed_mps = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    pit_depth_mm = models.IntegerField(null=True, blank=True)
    headroom_mm = models.IntegerField(null=True, blank=True)
    car_weight_kg = models.IntegerField(null=True, blank=True)

    # Manufacturing and installation ----------------------------------------
    brand = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    manufacturer = models.CharField(max_length=150, blank=True)
    installer = models.CharField(max_length=150, blank=True)
    installation_date = models.DateField(null=True, blank=True)
    commissioning_date = models.DateField(null=True, blank=True)
    ce_certificate_number = models.CharField(max_length=60, blank=True)
    warranty_end_date = models.DateField(null=True, blank=True)

    # Periodic inspection — fields only in phase 1, no module yet -------------
    last_inspection_date = models.DateField(null=True, blank=True)
    inspection_label = models.CharField(
        max_length=16, choices=InspectionLabel.choices, default=InspectionLabel.NONE
    )
    next_inspection_date = models.DateField(null=True, blank=True)
    inspection_body = models.CharField(max_length=150, blank=True)
    inspection_report_number = models.CharField(max_length=60, blank=True)

    # Status -----------------------------------------------------------------
    status = models.CharField(
        max_length=32, choices=ElevatorStatus.choices, default=ElevatorStatus.UNCONTRACTED
    )
    # Monthly maintenance is a legal requirement, so 30 is both the default and
    # the ceiling.
    maintenance_interval_days = models.SmallIntegerField(default=30)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "elevator"
        ordering = ["name"]
        constraints = [
            # Every unique key carries the soft-delete condition, or a deleted
            # record holds its registration number forever and the user is told
            # it is taken by something they cannot see.
            models.UniqueConstraint(
                fields=["company", "registration_number"],
                condition=models.Q(is_deleted=False) & ~models.Q(registration_number=""),
                name="uq_elevator_registration_number_active",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=ElevatorStatus.values),
                name="elevator_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(maintenance_interval_days__gte=1)
                & models.Q(maintenance_interval_days__lte=30),
                name="elevator_maintenance_interval_range",
            ),
            models.CheckConstraint(
                condition=models.Q(capacity_kg__isnull=True) | models.Q(capacity_kg__gt=0),
                name="elevator_capacity_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(stop_count__isnull=True)
                | (models.Q(stop_count__gte=2) & models.Q(stop_count__lte=100)),
                name="elevator_stop_count_range",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["building"]),
            models.Index(fields=["next_inspection_date"]),
        ]

    def __str__(self) -> str:
        return self.name or self.registration_number or str(self.pk)
