"""Recording inspections, and what they do to the lift.

- **The latest inspection drives the elevator.** Its five Phase 1 inspection
  fields are copied from the newest inspection on every save, so the elevator
  form shows what the inspector last said and cannot disagree with it.
- **A red label stops the lift.** The regulation forbids its use, so an active
  lift goes out of service; the next non-red result brings it back. A seal stays
  the office's manual `sealed` status.
- **Deadlines come from rules.py**, counted from the original periodic
  inspection — a follow-up never restarts the clock.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from django.db import transaction
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.attachments.services import link_attachment
from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults import services as fault_services
from apps.faults.models import Fault, FaultPriority
from apps.inspections import rules
from apps.inspections.models import (
    Inspection,
    InspectionKind,
    Label,
    NonConformity,
    NonConformityStatus,
)
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.scheduling import local_today

_OFFICE = (Role.OWNER, Role.ADMIN, Role.OPERATIONS)


class NonConformityEntry(TypedDict):
    description: str
    severity: str


# Derivations ------------------------------------------------------------------


def origin_of(inspection: Inspection) -> Inspection:
    """The periodic inspection a deadline counts from."""
    return inspection.follows if inspection.follows is not None else inspection


def derive_deadline(inspection: Inspection) -> date | None:
    return rules.correction_deadline(
        label=inspection.label,
        counted_from=origin_of(inspection).inspected_on,
        next_inspection_due=inspection.next_inspection_due,
    )


def default_next_inspection_due(*, inspected_on: date, follows: Inspection | None) -> date:
    """A year on — or, for a follow-up, the periodic cycle it belongs to.

    A follow-up checks the corrections; it is not the year's inspection, and
    the next periodic one stays when it was.
    """
    if follows is not None:
        return follows.next_inspection_due
    return rules.default_next_inspection(inspected_on)


def open_nonconformities(inspection: Inspection) -> QuerySet[NonConformity]:
    """Open defects that this inspection's deadline is about.

    Its own, and for a follow-up those of the periodic inspection it follows: a
    follow-up that turns red into yellow need not list the defects again for
    them to still be outstanding.
    """
    origin = origin_of(inspection)
    return NonConformity.objects.filter(
        inspection_id__in={inspection.pk, origin.pk}, status=NonConformityStatus.OPEN
    )


def current_inspections() -> QuerySet[Inspection]:
    """Inspections no later inspection of the same lift has superseded."""
    later = Inspection.objects.filter(
        elevator_id=OuterRef("elevator_id"), inspected_on__gt=OuterRef("inspected_on")
    )
    return Inspection.objects.annotate(superseded=Exists(later)).filter(superseded=False)


# The elevator -----------------------------------------------------------------


def sync_elevator(elevator: Elevator) -> None:
    """Copy the latest inspection onto the elevator, and stop or restore it.

    With no inspection recorded the Phase 1 values are left as they were typed:
    they are the only record there is.
    """
    latest = (
        Inspection.objects.filter(elevator=elevator)
        .order_by("-inspected_on", "-created_at", "-id")
        .first()
    )
    if latest is None:
        return
    previous_label = elevator.inspection_label
    elevator.last_inspection_date = latest.inspected_on
    elevator.inspection_label = latest.label
    elevator.next_inspection_date = latest.next_inspection_due
    elevator.inspection_body = latest.inspection_body
    elevator.inspection_report_number = latest.report_number
    fields = [
        "last_inspection_date",
        "inspection_label",
        "next_inspection_date",
        "inspection_body",
        "inspection_report_number",
    ]
    usable = rules.RULES[Label(latest.label)].usable
    if not usable and elevator.status == ElevatorStatus.ACTIVE:
        elevator.status = ElevatorStatus.OUT_OF_SERVICE
        fields.append("status")
    elif (
        usable and previous_label == Label.RED and elevator.status == ElevatorStatus.OUT_OF_SERVICE
    ):
        # Only a lift the red label stopped: one sealed by the municipality, or
        # suspended by the office, stays as a person left it.
        elevator.status = ElevatorStatus.ACTIVE
        fields.append("status")
    elevator.save(update_fields=[*fields, "updated_at"])


# Who hears about it -----------------------------------------------------------


def office(company_id: Any) -> list[User]:
    return list(User.objects.filter(company_id=company_id, role__in=_OFFICE, is_active=True))


def office_and_technicians(elevator: Elevator) -> list[User]:
    """The office, and the technicians who cover the lift's customer."""
    technicians = User.objects.filter(
        company_id=elevator.company_id,
        role=Role.TECHNICIAN,
        is_active=True,
        customer_assignments__customer_id=elevator.building.customer_id,
        customer_assignments__is_deleted=False,
    ).distinct()
    return [*office(elevator.company_id), *technicians]


def facts(inspection: Inspection) -> dict[str, Any]:
    elevator = inspection.elevator
    return {
        "elevator_name": elevator.name,
        "building_name": elevator.building.name,
        "label": inspection.label,
        "inspected_on": inspection.inspected_on.isoformat(),
        "correction_deadline": (
            inspection.correction_deadline.isoformat() if inspection.correction_deadline else None
        ),
    }


def _announce_red_label(inspection: Inspection) -> None:
    notify(
        kind=NotificationKind.INSPECTION_RED_LABEL,
        recipients=office_and_technicians(inspection.elevator),
        subject_type="elevator",
        subject_id=inspection.elevator_id,
        data=facts(inspection),
        dedupe_key=f"inspection_red_label:{inspection.pk}",
    )


# Recording --------------------------------------------------------------------


@transaction.atomic
def record(
    *,
    elevator: Elevator,
    inspected_on: date,
    label: str,
    by: User,
    kind: str = InspectionKind.PERIODIC,
    follows: Inspection | None = None,
    inspection_body: str = "",
    report_number: str = "",
    next_inspection_due: date | None = None,
    nonconformities: list[NonConformityEntry] | None = None,
) -> Inspection:
    """Record what the inspector found, and let it drive the lift."""
    inspection = Inspection(
        company_id=elevator.company_id,
        elevator=elevator,
        kind=kind,
        follows=follows,
        inspected_on=inspected_on,
        label=label,
        inspection_body=inspection_body,
        report_number=report_number,
        next_inspection_due=next_inspection_due
        or default_next_inspection_due(inspected_on=inspected_on, follows=follows),
        created_by=by,
        updated_by=by,
    )
    inspection.correction_deadline = derive_deadline(inspection)
    inspection.save()
    for entry in nonconformities or []:
        NonConformity.objects.create(
            company_id=inspection.company_id,
            inspection=inspection,
            description=entry["description"],
            severity=entry["severity"],
            created_by=by,
            updated_by=by,
        )
    sync_elevator(elevator)
    if label == Label.RED:
        _announce_red_label(inspection)
    return inspection


def attach_report(inspection: Inspection, attachment: Attachment) -> None:
    """Make `attachment` the inspection's report PDF.

    The same integrity rule as a contract's signed document
    (attachments.services.link_attachment): the file must be this
    inspection's own, in this company. On top of that it must be filed as an
    inspection report — a photo of the car is not the report.
    """
    if (
        attachment.object_type != ObjectType.INSPECTION
        or attachment.category != AttachmentCategory.INSPECTION_REPORT
    ):
        raise BusinessRuleError(ErrorCode.ATTACHMENT_TARGET_MISMATCH)
    link_attachment(inspection, attachment, "report")


@transaction.atomic
def edit(inspection: Inspection, *, changes: dict[str, Any], by: User) -> Inspection:
    """Correct an inspection; the deadline and the lift follow."""
    was_red = inspection.label == Label.RED
    report = changes.pop("report", ...)
    for name, value in changes.items():
        setattr(inspection, name, value)
    inspection.correction_deadline = derive_deadline(inspection)
    inspection.updated_by = by
    inspection.save()
    if report is not ...:
        if report is None:
            inspection.report = None
            inspection.save(update_fields=["report", "updated_at"])
        else:
            attach_report(inspection, report)
    sync_elevator(inspection.elevator)
    if inspection.label == Label.RED and not was_red:
        _announce_red_label(inspection)
    return inspection


@transaction.atomic
def add_nonconformity(
    inspection: Inspection, *, description: str, severity: str, by: User
) -> NonConformity:
    return NonConformity.objects.create(
        company_id=inspection.company_id,
        inspection=inspection,
        description=description,
        severity=severity,
        created_by=by,
        updated_by=by,
    )


@transaction.atomic
def edit_nonconformity(
    nonconformity: NonConformity, *, changes: dict[str, Any], by: User
) -> NonConformity:
    """Mark a defect fixed or waived, or reopen it.

    Fixed with no date means fixed today; reopened clears the date, which would
    otherwise claim a fix that no longer stands.
    """
    for name, value in changes.items():
        setattr(nonconformity, name, value)
    if nonconformity.status == NonConformityStatus.FIXED and nonconformity.fixed_on is None:
        nonconformity.fixed_on = local_today()
    if nonconformity.status == NonConformityStatus.OPEN:
        nonconformity.fixed_on = None
    nonconformity.updated_by = by
    nonconformity.save()
    return nonconformity


@transaction.atomic
def open_fault(nonconformity: NonConformity, *, by: User) -> Fault:
    """Open an M4 fault on the lift to fix this defect, and link the two.

    High priority for a red defect (the lift may not run until it is fixed),
    normal otherwise. Converges: a defect that already has a fault answers with
    it rather than opening a second one, so a double tap is harmless.
    """
    nonconformity = (
        NonConformity.objects.select_for_update(of=("self",))
        .select_related("inspection__elevator", "fault")
        .get(pk=nonconformity.pk)
    )
    if nonconformity.fault is not None:
        return nonconformity.fault
    fault = fault_services.report_by_staff(
        elevator=nonconformity.inspection.elevator,
        description=nonconformity.description[:1000],
        priority=(
            FaultPriority.HIGH if nonconformity.severity == Label.RED else FaultPriority.NORMAL
        ),
        reported_by=by,
    )
    nonconformity.fault = fault
    nonconformity.updated_by = by
    nonconformity.save(update_fields=["fault", "updated_by", "updated_at"])
    return fault


def has_open_nonconformities() -> Q:
    """For filtering inspections: those with an open defect of their own."""
    return Q(
        Exists(
            NonConformity.objects.filter(
                inspection_id=OuterRef("pk"), status=NonConformityStatus.OPEN
            )
        )
    )


# The open correction deadline, on the elevator ------------------------------------


def with_open_correction(queryset: QuerySet[Elevator]) -> QuerySet[Elevator]:
    """Annotate each lift with its current inspection's open correction.

    `current_correction_deadline` and `current_open_nonconformities`, read in the
    list's own query rather than one query per row: the current inspection is
    the lift's latest, and its open defects include those of the periodic
    inspection it follows, as everywhere else (`open_nonconformities`).
    """
    latest = Inspection.objects.filter(elevator_id=OuterRef("pk")).order_by(
        "-inspected_on", "-created_at", "-id"
    )
    annotated = queryset.annotate(
        current_inspection_id=Subquery(latest.values("id")[:1]),
        current_inspection_follows_id=Subquery(latest.values("follows_id")[:1]),
        current_correction_deadline=Subquery(latest.values("correction_deadline")[:1]),
    )
    still_open = (
        NonConformity.objects.filter(status=NonConformityStatus.OPEN)
        .filter(
            Q(inspection_id=OuterRef("current_inspection_id"))
            | Q(inspection_id=OuterRef("current_inspection_follows_id"))
        )
        .order_by()
        .values("status")
        .annotate(count=Count("id"))
        .values("count")[:1]
    )
    return annotated.annotate(
        current_open_nonconformities=Coalesce(
            Subquery(still_open, output_field=IntegerField()), Value(0)
        )
    )


def open_correction(elevator: Elevator) -> tuple[date | None, int]:
    """(deadline, open defects) of the lift's current inspection.

    The deadline only while a defect is open: a red label whose defects are all
    fixed has nothing left to correct, whatever date the label carried.
    """
    annotated = getattr(elevator, "current_open_nonconformities", None)
    if annotated is not None:
        deadline = getattr(elevator, "current_correction_deadline", None)
        count = int(annotated)
    else:
        latest = (
            Inspection.objects.filter(elevator=elevator)
            .select_related("follows")
            .order_by("-inspected_on", "-created_at", "-id")
            .first()
        )
        if latest is None:
            return None, 0
        deadline, count = latest.correction_deadline, open_nonconformities(latest).count()
    if count == 0 or deadline is None:
        return None, count
    return deadline, count
