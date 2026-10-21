"""The fault's life: reporting, the transitions, billing, and who hears of it.

Every transition here is atomic, locks the row it moves, and refuses to run
from the wrong state with 409 FAULT_TRANSITION_NOT_ALLOWED. The checks live
here rather than in the view so that the M6 customer portal and anything else
that moves a fault gets the same state machine without re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.contracts.models import ContractElevator, ContractStatus, Scope
from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults.models import (
    CANCELLABLE,
    Fault,
    FaultChannel,
    FaultPriority,
    FaultStatus,
    new_reference,
)
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User
from core.context import company_context
from core.error_codes import ErrorCode
from core.exceptions import Conflict
from core.scheduling import LOCAL_TZ

#: Repeated public reports of one lift within this window are one fault.
PUBLIC_MERGE_WINDOW = timedelta(minutes=30)
#: How long a public reporter's name and phone outlive the closed fault.
REPORTER_DATA_RETENTION = timedelta(days=90)

_OFFICE = (Role.OWNER, Role.ADMIN, Role.OPERATIONS)
_REFERENCE_ATTEMPTS = 8


def _refuse() -> Conflict:
    return Conflict(ErrorCode.FAULT_TRANSITION_NOT_ALLOWED)


def _locked(fault: Fault) -> Fault:
    """The row again, locked for the rest of the transaction.

    Two people pressing "assign" at once must not both succeed from `open`;
    the second reads the state the first left behind. SQLite ignores the lock,
    which is fine for a single-process test run.
    """
    return (
        Fault.objects.select_for_update(of=("self",))
        .select_related("elevator__building")
        .get(pk=fault.pk)
    )


# Who hears about it -------------------------------------------------------------


def _office(company_id: Any) -> list[User]:
    return list(User.objects.filter(company_id=company_id, role__in=_OFFICE, is_active=True))


def _customer_technicians(fault: Fault) -> list[User]:
    return list(
        User.objects.filter(
            company_id=fault.company_id,
            role=Role.TECHNICIAN,
            is_active=True,
            customer_assignments__customer_id=fault.elevator.building.customer_id,
            customer_assignments__is_deleted=False,
        ).distinct()
    )


def _facts(fault: Fault) -> dict[str, Any]:
    return {
        "reference": fault.reference,
        "elevator_name": fault.elevator.name,
        "building_name": fault.elevator.building.name,
        "priority": fault.priority,
    }


def _announce_report(fault: Fault) -> None:
    """`fault_reported`, or `fault_emergency` for a trapped passenger.

    One or the other, not both: the emergency goes to the same people (plus
    whoever is already assigned) and an extra ordinary notice beside it would
    only dilute it. The reporter is left out — they know.
    """
    emergency = fault.priority == FaultPriority.EMERGENCY
    kind = NotificationKind.FAULT_EMERGENCY if emergency else NotificationKind.FAULT_REPORTED
    people = {user.pk: user for user in [*_office(fault.company_id), *_customer_technicians(fault)]}
    if emergency and fault.technician is not None:
        people[fault.technician.pk] = fault.technician
    if fault.reported_by_id is not None:
        people.pop(fault.reported_by_id, None)
    notify(
        kind=kind,
        recipients=people.values(),
        subject_type="fault",
        subject_id=fault.pk,
        data={
            **_facts(fault),
            "channel": fault.channel,
            "description": fault.description[:200],
        },
        dedupe_key=f"{kind}:{fault.pk}",
    )


def _announce_assignment(fault: Fault) -> None:
    technician = fault.technician
    assert technician is not None and fault.assigned_at is not None
    # The person who assigned it to themselves does not need telling.
    if technician.pk == fault.reported_by_id and fault.channel == FaultChannel.STAFF:
        return
    notify(
        kind=NotificationKind.FAULT_ASSIGNED,
        recipients=[technician],
        subject_type="fault",
        subject_id=fault.pk,
        data=_facts(fault),
        # Per assignment: a reassignment back to the same person is new news.
        dedupe_key=f"fault_assigned:{fault.pk}:{fault.assigned_at.isoformat()}",
    )


def _announce_resolution(fault: Fault) -> None:
    technician = fault.technician
    notify(
        kind=NotificationKind.FAULT_RESOLVED,
        recipients=_office(fault.company_id),
        subject_type="fault",
        subject_id=fault.pk,
        data={
            **_facts(fault),
            "technician_name": technician.full_name if technician else "",
        },
        dedupe_key=f"fault_resolved:{fault.pk}",
    )


# Creating ---------------------------------------------------------------------


def _insert(**values: Any) -> Fault:
    """Create with a fresh reference, retrying the rare collision.

    Each attempt in its own savepoint, so a collision on PostgreSQL does not
    abort the caller's transaction.
    """
    for _ in range(_REFERENCE_ATTEMPTS):
        try:
            with transaction.atomic():
                return Fault.objects.create(reference=new_reference(), **values)
        except IntegrityError:
            continue
    raise RuntimeError("Could not allocate a fault reference.")  # pragma: no cover


def _stop(elevator: Elevator) -> None:
    # Only an active lift is put out of service: one already sealed or
    # suspended has a status that means more than "a fault stopped it".
    if elevator.status == ElevatorStatus.ACTIVE:
        elevator.status = ElevatorStatus.OUT_OF_SERVICE
        elevator.save(update_fields=["status", "updated_at"])


@transaction.atomic
def report_by_staff(
    *,
    elevator: Elevator,
    description: str,
    priority: str,
    reported_by: User,
    technician: User | None = None,
    stopped_elevator: bool = False,
    someone_trapped: bool = False,
) -> Fault:
    """A fault entered by somebody at the firm (`channel=staff`).

    With a technician it starts `assigned`; a trapped passenger is always
    `emergency`, whatever priority was sent.
    """
    now = timezone.now()
    fault = _insert(
        company_id=elevator.company_id,
        elevator=elevator,
        channel=FaultChannel.STAFF,
        reported_by=reported_by,
        description=description,
        priority=FaultPriority.EMERGENCY if someone_trapped else priority,
        status=FaultStatus.ASSIGNED if technician else FaultStatus.OPEN,
        technician=technician,
        reported_at=now,
        assigned_at=now if technician else None,
        stopped_elevator=stopped_elevator,
        created_by=reported_by,
        updated_by=reported_by,
    )
    if stopped_elevator:
        _stop(elevator)
    _announce_report(fault)
    if technician is not None:
        _announce_assignment(fault)
    return fault


@dataclass(frozen=True)
class PublicReport:
    description: str
    someone_trapped: bool = False
    elevator_stopped: bool = False
    reporter_name: str = ""
    reporter_phone: str = ""


def public_priority(report: PublicReport) -> str:
    """The only way the public sets a priority: by what they say happened."""
    if report.someone_trapped:
        return FaultPriority.EMERGENCY
    if report.elevator_stopped:
        return FaultPriority.HIGH
    return FaultPriority.NORMAL


_PRIORITY_RANK: dict[str, int] = {
    FaultPriority.EMERGENCY: 0,
    FaultPriority.HIGH: 1,
    FaultPriority.NORMAL: 2,
}


def report_public(*, elevator: Elevator, report: PublicReport) -> Fault:
    """A report from the QR sticker, by somebody with no account.

    The token resolved to exactly one firm, so the work runs in that firm's
    tenant context and nothing here can reach another's rows.

    Ten residents tapping the sticker make one fault: a report on a lift that
    already has an open or assigned public fault from the last 30 minutes is
    appended to it, and the existing reference is returned. A merged report can
    raise the priority (somebody is now trapped) but never lower it. Its name
    and phone are kept only if the fault has none yet, so the personal data
    stays in the two columns the retention job blanks.
    """
    with company_context(elevator.company_id), transaction.atomic():
        now = timezone.now()
        priority = public_priority(report)
        existing = (
            Fault.objects.select_for_update(of=("self",))
            .select_related("elevator__building", "technician")
            .filter(
                elevator=elevator,
                channel=FaultChannel.PUBLIC_QR,
                status__in=[FaultStatus.OPEN, FaultStatus.ASSIGNED],
                reported_at__gte=now - PUBLIC_MERGE_WINDOW,
            )
            .order_by("-reported_at", "-id")
            .first()
        )
        if existing is not None:
            return _merge(existing, report=report, priority=priority, at=now)

        fault = _insert(
            company_id=elevator.company_id,
            elevator=elevator,
            channel=FaultChannel.PUBLIC_QR,
            reporter_name=report.reporter_name,
            reporter_phone=report.reporter_phone,
            description=report.description,
            priority=priority,
            reported_at=now,
            stopped_elevator=report.elevator_stopped,
        )
        if report.elevator_stopped:
            _stop(elevator)
        _announce_report(fault)
        return fault


def report_by_portal(*, account: Any, elevator: Elevator, report: PublicReport) -> Fault:
    """A report from a customer contact signed in to the portal (M6).

    The public report's fields and priority mapping, without its honeypot or its
    30-minute merge: the contact is identified, and two reports from a person
    who can be called back are two things they chose to say. Runs in the
    caller's tenant context, which the portal view has set to the account's
    firm.
    """
    with transaction.atomic():
        fault = _insert(
            company_id=elevator.company_id,
            elevator=elevator,
            channel=FaultChannel.CUSTOMER_PORTAL,
            reported_by_portal_account=account,
            description=report.description,
            priority=public_priority(report),
            reported_at=timezone.now(),
            stopped_elevator=report.elevator_stopped,
        )
        if report.elevator_stopped:
            _stop(elevator)
        _announce_report(fault)
    return fault


def _merge(fault: Fault, *, report: PublicReport, priority: str, at: datetime) -> Fault:
    fault.additional_reports = [
        *fault.additional_reports,
        {
            "reported_at": at.isoformat(),
            "description": report.description,
            "someone_trapped": report.someone_trapped,
            "elevator_stopped": report.elevator_stopped,
        },
    ]
    fields = ["additional_reports", "updated_at"]
    if not fault.reporter_name and not fault.reporter_phone:
        fault.reporter_name, fault.reporter_phone = report.reporter_name, report.reporter_phone
        fields += ["reporter_name", "reporter_phone"]
    escalated = _PRIORITY_RANK[priority] < _PRIORITY_RANK[fault.priority]
    if escalated:
        fault.priority = priority
        fields.append("priority")
    if report.elevator_stopped and not fault.stopped_elevator:
        fault.stopped_elevator = True
        fields.append("stopped_elevator")
        _stop(fault.elevator)
    fault.save(update_fields=fields)
    if escalated and priority == FaultPriority.EMERGENCY:
        _announce_report(fault)
    return fault


# Transitions ------------------------------------------------------------------


@transaction.atomic
def assign(fault: Fault, *, technician: User, by: User) -> Fault:
    """Send somebody, or send somebody else. From `open` or `assigned`."""
    fault = _locked(fault)
    if fault.status not in (FaultStatus.OPEN, FaultStatus.ASSIGNED):
        raise _refuse()
    if fault.status == FaultStatus.ASSIGNED and fault.technician_id == technician.pk:
        return fault
    fault.technician = technician
    fault.status = FaultStatus.ASSIGNED
    fault.assigned_at = timezone.now()
    fault.updated_by = by
    fault.save(update_fields=["technician", "status", "assigned_at", "updated_by", "updated_at"])
    _announce_assignment(fault)
    return fault


@transaction.atomic
def arrive(fault: Fault, *, by: User) -> Fault:
    fault = _locked(fault)
    if fault.status != FaultStatus.ASSIGNED:
        raise _refuse()
    fault.status = FaultStatus.ON_SITE
    fault.on_site_at = timezone.now()
    fault.updated_by = by
    fault.save(update_fields=["status", "on_site_at", "updated_by", "updated_at"])
    return fault


def covering_scope(elevator_id: Any, day: date) -> str | None:
    """The scope of the contract that covered the lift on `day`, if any.

    The contract as it stood on the day of the report, not today's: a fault
    reported under a full-coverage contract that has since expired was still
    covered. A draft never covered anything.
    """
    line = (
        ContractElevator.objects.filter(
            elevator_id=elevator_id,
            added_at__lte=day,
            contract__start_date__lte=day,
            contract__end_date__gte=day,
            contract__is_deleted=False,
        )
        .filter(Q(removed_at__isnull=True) | Q(removed_at__gte=day))
        .exclude(contract__status=ContractStatus.DRAFT)
        .select_related("contract")
        .order_by("-added_at", "-id")
        .first()
    )
    return line.contract.scope if line else None


def derive_billable(fault: Fault) -> bool:
    """Whether the firm bills this repair, by the contract on the day of the report.

    - maintenance only: yes — repairs are not covered;
    - maintenance and repair: only when parts were used — labour is covered;
    - full coverage: no;
    - no contract: yes.
    """
    scope = covering_scope(
        fault.elevator_id, timezone.localtime(fault.reported_at, LOCAL_TZ).date()
    )
    if scope == Scope.FULL_COVERAGE:
        return False
    if scope == Scope.MAINTENANCE_AND_REPAIR:
        return bool(fault.parts_used.strip())
    return True


@transaction.atomic
def resolve(
    fault: Fault,
    *,
    by: User,
    resolution_notes: str = "",
    parts_used: str = "",
    restore_service: bool = False,
) -> Fault:
    """The work is done. From `on_site` only.

    Derives `billable` unless the office has already stated it; an override is a
    decision and the derivation does not get to undo it. With `restore_service`
    an out-of-service lift goes back to active.
    """
    fault = _locked(fault)
    if fault.status != FaultStatus.ON_SITE:
        raise _refuse()
    fault.status = FaultStatus.RESOLVED
    fault.resolved_at = timezone.now()
    fault.resolution_notes = resolution_notes
    fault.parts_used = parts_used
    if not fault.billable_overridden:
        fault.billable = derive_billable(fault)
    fault.updated_by = by
    fault.save(
        update_fields=[
            "status",
            "resolved_at",
            "resolution_notes",
            "parts_used",
            "billable",
            "updated_by",
            "updated_at",
        ]
    )
    elevator = fault.elevator
    if restore_service and elevator.status == ElevatorStatus.OUT_OF_SERVICE:
        elevator.status = ElevatorStatus.ACTIVE
        elevator.save(update_fields=["status", "updated_at"])
    _announce_resolution(fault)
    return fault


@transaction.atomic
def close(fault: Fault, *, by: User) -> Fault:
    """The office has checked the work. Billing (M7) reads only closed faults."""
    fault = _locked(fault)
    if fault.status != FaultStatus.RESOLVED:
        raise _refuse()
    fault.status = FaultStatus.CLOSED
    fault.closed_at = timezone.now()
    fault.updated_by = by
    fault.save(update_fields=["status", "closed_at", "updated_by", "updated_at"])
    return fault


@transaction.atomic
def cancel(fault: Fault, *, reason: str, by: User) -> Fault:
    """A duplicate, a false alarm. Any state before `resolved`.

    The lift's status is left alone: whether it can run again is a judgement
    for whoever looks at it, and another fault may be what stopped it.
    """
    fault = _locked(fault)
    if fault.status not in CANCELLABLE:
        raise _refuse()
    fault.status = FaultStatus.CANCELLED
    fault.cancelled_at = timezone.now()
    fault.cancel_reason = reason
    fault.updated_by = by
    fault.save(
        update_fields=["status", "cancelled_at", "cancel_reason", "updated_by", "updated_at"]
    )
    return fault


@transaction.atomic
def edit(fault: Fault, *, changes: dict[str, Any], by: User) -> Fault:
    """The office's corrections: priority, description, billable.

    Stating `billable` marks it overridden, which resolve() then respects and
    the screen shows. Raising the priority to emergency tells people the way a
    new emergency would.
    """
    fault = _locked(fault)
    was_emergency = fault.priority == FaultPriority.EMERGENCY
    for name, value in changes.items():
        setattr(fault, name, value)
    if "billable" in changes:
        fault.billable_overridden = True
    fault.updated_by = by
    fault.save()
    if fault.priority == FaultPriority.EMERGENCY and not was_emergency:
        _announce_report(fault)
    return fault


# Retention --------------------------------------------------------------------


def forget_public_reporters(*, now: datetime | None = None) -> int:
    """Blank the name and phone of public reports finished over 90 days ago.

    Closed, or cancelled — a cancelled report holds the same personal data and
    has even less reason to keep it. A queryset update on purpose: no audit
    entry is wanted (the trail masks these two fields anyway, so there is
    nothing in it to correct), and the rows are many and identical in what
    changes. Runs across every firm; the caller opens the system context.
    """
    cutoff = (now or timezone.now()) - REPORTER_DATA_RETENTION
    finished = Q(status=FaultStatus.CLOSED, closed_at__lt=cutoff) | Q(
        status=FaultStatus.CANCELLED, cancelled_at__lt=cutoff
    )
    holding = ~Q(reporter_name="") | ~Q(reporter_phone="")
    return int(
        Fault.objects.filter(finished, holding, channel=FaultChannel.PUBLIC_QR).update(
            reporter_name="", reporter_phone="", updated_at=timezone.now()
        )
    )
