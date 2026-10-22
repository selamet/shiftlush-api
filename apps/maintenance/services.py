"""Business rules for a maintenance visit, and for the monthly plan.

Two things live here rather than in the serializer: whether the named
contract actually backs this elevator (a question about the world, not
about the shape of the request), and keeping Elevator.next_maintenance_date
correct as visits are created, edited, and deleted - always recomputed from
the surviving rows, never incremented in place, so a backdated or deleted
visit cannot leave a stale date behind.

The plan (M3, docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md)
follows the same rule: a PlannedVisit's status is recomputed from the visits
that survive in its month, so the plan and the record cannot disagree however
a visit is edited or deleted.
"""

from __future__ import annotations

import calendar
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import NotRequired, TypedDict

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.contracts.models import Contract, ContractElevator, ContractStatus
from apps.elevators.models import Elevator
from apps.maintenance.models import (
    MaintenanceVisit,
    MaintenanceVisitItem,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User, UserCustomer
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.scheduling import local_today


class ChecklistEntry(TypedDict):
    item: str
    result: str
    note: NotRequired[str | None]


def ensure_active_contract(*, elevator: Elevator, contract: Contract) -> None:
    if contract.status != ContractStatus.ACTIVE:
        raise BusinessRuleError(ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR)
    covers = ContractElevator.objects.filter(
        contract=contract, elevator=elevator, removed_at__isnull=True
    ).exists()
    if not covers:
        raise BusinessRuleError(ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR)


def recalculate_next_maintenance(elevator: Elevator) -> None:
    latest = (
        MaintenanceVisit.objects.filter(elevator=elevator, status=VisitStatus.COMPLETED)
        .order_by("-visit_date", "-id")
        .first()
    )
    if latest is None:
        elevator.last_maintenance_date = None
        elevator.next_maintenance_date = None
    else:
        elevator.last_maintenance_date = latest.visit_date
        elevator.next_maintenance_date = latest.visit_date + timedelta(
            days=elevator.maintenance_interval_days
        )
    elevator.save(update_fields=["last_maintenance_date", "next_maintenance_date", "updated_at"])
    refresh_planned_due_dates(elevator)


def refresh_planned_due_dates(elevator: Elevator, *, today: date | None = None) -> int:
    """Move the due date of the lift's open plan rows to follow its record.

    Called whenever `next_maintenance_date` is recalculated, so the plan and
    the lift never disagree: before this, rows were re-derived only when a visit
    in their own month changed, and a visit recorded against another month left
    the row on its old date. Only rows nobody has edited, still planned, in this
    month or later: an edited row is a person's decision, and a missed row of a
    past month is history. Returns how many rows moved.
    """
    current = period_of(today or local_today())
    moved = 0
    rows = PlannedVisit.objects.filter(
        elevator_id=elevator.pk,
        status=PlannedVisitStatus.PLANNED,
        manually_edited=False,
        period__gte=current,
    )
    for row in rows:
        due = due_date_in(row.period, elevator.next_maintenance_date)
        if row.due_date != due:
            row.due_date = due
            row.save(update_fields=["due_date", "updated_at"])
            moved += 1
    return moved


@transaction.atomic
def set_checklist(
    *, visit: MaintenanceVisit, items: list[ChecklistEntry]
) -> list[MaintenanceVisitItem]:
    """Replace the visit's checklist rows with exactly this set.

    Closing the old rows and opening new ones, the same shape
    apps.contracts.services already uses for renew()/terminate() - not an
    in-place update, so a soft-deleted row still tells the truth about what
    it originally said.
    """
    visit.items.all().delete()
    return [
        MaintenanceVisitItem.objects.create(
            company_id=visit.company_id,
            visit=visit,
            item=entry["item"],
            result=entry["result"],
            note=entry.get("note") or "",
        )
        for entry in items
    ]


# ---------------------------------------------------------------------------
# The monthly plan
# ---------------------------------------------------------------------------


def period_of(day: date) -> str:
    """The plan month a day belongs to, as "YYYY-MM"."""
    return f"{day.year:04d}-{day.month:02d}"


def month_bounds(period: str) -> tuple[date, date]:
    """First and last day of a "YYYY-MM" period."""
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def next_period(period: str) -> str:
    _, last = month_bounds(period)
    return period_of(last + timedelta(days=1))


def due_date_in(period: str, next_maintenance_date: date | None) -> date:
    """When a lift falls due, for its row in `period`.

    Its `next_maintenance_date`, as it is — including a date before the month:
    a lift that fell due on 8 September and has not been seen is overdue since
    8 September, and its October row says so. Clamping that to the 1st (which
    this used to do) printed "01.10" on every carried-over row and contradicted
    the lift's own record (SL-162, audit P1-01).

    A date past the month — a visit early in the month moved it on — is held to
    the month's last day, because this month's obligation is met by a visit at
    any point in it. A lift never visited has no date, and the middle of the
    month leaves room on both sides.
    """
    first, last = month_bounds(period)
    if next_maintenance_date is None:
        return first.replace(day=15)
    return min(next_maintenance_date, last)


def sync_planned_visit(*, elevator_id: uuid.UUID, day: date) -> None:
    """Bring the lift's row for `day`'s month in line with its visits.

    Recomputed from every surviving visit in the month rather than updated
    from the one that just changed. That is what makes deletion reopen a row,
    an edit that moves a visit into another month move the link, and a
    completed visit outrank a later failed attempt — without a case for each.

    - A completed visit in the month: `done`, linked to the latest completed.
    - Only attempts (incomplete, access denied): `planned`, linked to the
      latest attempt.
    - No visit: `planned`, unlinked.

    A cancelled row is left alone: the lift has no cover, and a visit recorded
    against it anyway does not revive an obligation. No row at all — an ad-hoc
    visit, a lift planned in another month — is not an error; the visit stands
    on its own.

    Called inside the visit's own transaction, from the viewset.
    """
    row = (
        PlannedVisit.objects.filter(elevator_id=elevator_id, period=period_of(day))
        .exclude(status=PlannedVisitStatus.CANCELLED)
        .first()
    )
    if row is None:
        return
    first, last = month_bounds(row.period)
    visits = list(
        MaintenanceVisit.objects.filter(
            elevator_id=elevator_id, visit_date__gte=first, visit_date__lte=last
        ).order_by("-visit_date", "-id")
    )
    completed = next((v for v in visits if v.status == VisitStatus.COMPLETED), None)
    visit: MaintenanceVisit | None
    if completed is not None:
        status, visit = PlannedVisitStatus.DONE, completed
    else:
        status, visit = PlannedVisitStatus.PLANNED, (visits[0] if visits else None)

    if row.status != status or row.visit_id != (visit.pk if visit else None):
        row.status = status
        row.visit = visit
        row.save(update_fields=["status", "visit", "updated_at"])


def sync_plan_for_visit(
    visit: MaintenanceVisit, *, before: tuple[uuid.UUID, date] | None = None
) -> None:
    """Re-sync every row a visit's create, edit or delete can have touched.

    `before` is the visit's (elevator, date) as it was before an edit. When the
    edit moved the visit to another lift or month, the row it used to close is
    re-synced too, which is what reopens it.
    """
    targets = {(visit.elevator_id, period_of(visit.visit_date)): visit.visit_date}
    if before is not None:
        targets.setdefault((before[0], period_of(before[1])), before[1])
    for (elevator_id, _), day in targets.items():
        sync_planned_visit(elevator_id=elevator_id, day=day)


# Assignments -----------------------------------------------------------------


def announce_assignments(assigned: Counter[tuple[uuid.UUID, str]], *, action_id: str) -> int:
    """One `visit_assigned` per technician and month for one assignment action.

    `assigned` counts the rows the action gave each (technician, period). The
    dedupe key carries the action, so the same action run twice — a job
    restarted mid-way — tells nobody twice, while the next action is news.
    Returns how many notifications were raised.
    """
    if not assigned:
        return 0
    users = User.objects.in_bulk({technician_id for technician_id, _ in assigned})
    raised = 0
    for (technician_id, period), count in sorted(assigned.items(), key=lambda i: str(i[0])):
        technician = users.get(technician_id)
        if technician is None:
            continue
        raised += len(
            notify(
                kind=NotificationKind.VISIT_ASSIGNED,
                recipients=[technician],
                subject_type="plan",
                subject_id=technician.pk,
                data={"period": period, "count": count},
                dedupe_key=f"visit_assigned:{technician_id}:{period}:{action_id}",
            )
        )
    return raised


@transaction.atomic
def edit_planned_visit(
    row: PlannedVisit, *, changes: dict[str, object], editor: User
) -> PlannedVisit:
    """Apply an office edit (technician, planned date) and mark the row edited.

    Marked even when the edit only sets a date: a person has looked at this row
    and decided something, and the generator must not overwrite it or cancel it
    behind their back.
    """
    previous_technician = row.technician_id
    for name, value in changes.items():
        setattr(row, name, value)
    row.manually_edited = True
    row.updated_by = editor
    row.save()
    if (
        row.technician_id is not None
        and row.technician_id != previous_technician
        and row.status == PlannedVisitStatus.PLANNED
        and not coalesce_assignment(row.technician_id, row.period)
    ):
        announce_assignments(
            Counter({(row.technician_id, row.period): 1}), action_id=f"edit:{uuid.uuid4()}"
        )
    return row


#: How long a technician's unread "N visits assigned" keeps counting new ones.
ASSIGNMENT_COALESCE_WINDOW = timedelta(minutes=15)


def coalesce_assignment(
    technician_id: uuid.UUID, period: str, *, now: datetime | None = None
) -> bool:
    """Fold one more assigned row into the technician's recent unread notice.

    The calendar assigns one drop at a time, and ten drops are not ten pieces
    of news: an unread `visit_assigned` for the same month, raised in the last
    `ASSIGNMENT_COALESCE_WINDOW`, counts one more instead, and no second e-mail
    goes out. A read one, an older one or another month's is left alone, and
    the assignment is announced as usual. Returns whether it folded.
    """
    since = (now or timezone.now()) - ASSIGNMENT_COALESCE_WINDOW
    recent = (
        Notification.unscoped.select_for_update()
        .filter(
            recipient_id=technician_id,
            kind=NotificationKind.VISIT_ASSIGNED,
            read_at__isnull=True,
            is_deleted=False,
            data__period=period,
            created_at__gte=since,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if recent is None:
        return False
    recent.data = {**recent.data, "count": int(recent.data.get("count", 0)) + 1}
    recent.save(update_fields=["data", "updated_at"])
    return True


@transaction.atomic
def bulk_assign(rows: Iterable[PlannedVisit], *, technician: User, editor: User) -> int:
    """Give every row to `technician`, all in one transaction. Returns the count.

    Rows already theirs are counted but not announced again: a retried request
    converges on the same plan and tells nobody twice. Only rows still planned
    are announced — assigning a done row is bookkeeping, not work.
    """
    action_id = f"bulk:{uuid.uuid4()}"
    announced: Counter[tuple[uuid.UUID, str]] = Counter()
    count = 0
    for row in rows:
        count += 1
        newly = row.technician_id != technician.pk
        row.technician = technician
        row.manually_edited = True
        row.updated_by = editor
        row.save(update_fields=["technician", "manually_edited", "updated_by", "updated_at"])
        if newly and row.status == PlannedVisitStatus.PLANNED:
            announced[(technician.pk, row.period)] += 1
    announce_assignments(announced, action_id=action_id)
    return count


# Generation ------------------------------------------------------------------


@dataclass
class PlanRun:
    """What one run of the generator did, for the job's return value and tests."""

    created: int = 0
    updated: int = 0
    cancelled: int = 0
    notified: int = 0
    assigned: Counter[tuple[uuid.UUID, str]] = field(default_factory=Counter)


def _covered_elevators() -> list[Elevator]:
    """Every lift an ACTIVE contract covers, in whatever tenant scope is open."""
    return list(
        Elevator.objects.filter(
            contract_lines__removed_at__isnull=True,
            contract_lines__is_deleted=False,
            contract_lines__contract__status=ContractStatus.ACTIVE,
            contract_lines__contract__is_deleted=False,
        )
        .select_related("building")
        .distinct()
        .order_by("id")
    )


def _default_technicians() -> dict[uuid.UUID, uuid.UUID]:
    """Customer -> its technician, for customers with exactly one active one.

    Two or more and the office decides; none and there is nobody to pick.
    """
    assigned: defaultdict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    rows = UserCustomer.objects.filter(
        user__role=Role.TECHNICIAN, user__is_active=True, user__is_deleted=False
    ).values_list("customer_id", "user_id")
    for customer_id, user_id in rows:
        assigned[customer_id].add(user_id)
    return {customer: next(iter(users)) for customer, users in assigned.items() if len(users) == 1}


def _create_row(**values: object) -> PlannedVisit | None:
    # Its own savepoint, so that a row a concurrent run inserted first is
    # skipped rather than aborting this run's transaction on PostgreSQL. The
    # unique (elevator, period) is the generator's whole idempotency.
    try:
        with transaction.atomic():
            return PlannedVisit.objects.create(**values)
    except IntegrityError:
        return None


@transaction.atomic
def generate_plan(*, today: date) -> PlanRun:
    """Make sure this month and next have one row per covered lift.

    Run daily by `plan_maintenance`, under the system context, across every
    firm. Idempotent: a second run the same day finds every row in place and
    changes nothing.

    - A covered lift with no row gets one, due per `due_date_in`, assigned to
      its customer's only technician if it has exactly one.
    - A row nobody has edited follows the facts: its due date tracks the lift's
      `next_maintenance_date` while it is still planned and unattempted, an
      unassigned one picks up a default technician that has since appeared, and
      a cancelled one comes back when its lift regains cover.
    - A planned, unedited row whose lift lost cover is cancelled. An edited one
      is left for a person to resolve; the screen flags it.

    One transaction, so the notifications — one per technician per month for
    this run — are enqueued only once the plan they describe has committed.
    """
    run = PlanRun()
    covered = _covered_elevators()
    covered_ids = {elevator.pk for elevator in covered}
    defaults = _default_technicians()
    action_id = f"plan:{today.isoformat()}"

    this_month = period_of(today)
    for period in (this_month, next_period(this_month)):
        existing = {row.elevator_id: row for row in PlannedVisit.objects.filter(period=period)}

        for elevator in covered:
            due = due_date_in(period, elevator.next_maintenance_date)
            default = defaults.get(elevator.building.customer_id)
            row = existing.get(elevator.pk)
            if row is None:
                created = _create_row(
                    company_id=elevator.company_id,
                    elevator=elevator,
                    period=period,
                    due_date=due,
                    technician_id=default,
                )
                if created is not None:
                    run.created += 1
                    if default is not None:
                        run.assigned[(default, period)] += 1
                continue
            if row.manually_edited:
                continue
            fields = _follow_the_facts(row, due=due, default=default)
            if fields:
                row.save(update_fields=[*fields, "updated_at"])
                run.updated += 1
                if "status" in fields:
                    # Reopened: a visit recorded while it was cancelled may
                    # already have done this month's work.
                    sync_planned_visit(elevator_id=row.elevator_id, day=month_bounds(period)[0])
                if "technician" in fields and row.technician_id is not None:
                    run.assigned[(row.technician_id, period)] += 1

        for row in existing.values():
            if (
                row.elevator_id not in covered_ids
                and row.status == PlannedVisitStatus.PLANNED
                and not row.manually_edited
            ):
                row.status = PlannedVisitStatus.CANCELLED
                row.save(update_fields=["status", "updated_at"])
                run.cancelled += 1

    run.notified = announce_assignments(run.assigned, action_id=action_id)
    return run


def _follow_the_facts(row: PlannedVisit, *, due: date, default: uuid.UUID | None) -> list[str]:
    """Update an unedited row of a covered lift in place; return what changed."""
    changed: list[str] = []
    if row.status == PlannedVisitStatus.CANCELLED:
        row.status = PlannedVisitStatus.PLANNED
        changed.append("status")
    # An attempt linked to the row does not freeze its date: the lift is still
    # due when its record says it is.
    if row.status == PlannedVisitStatus.PLANNED and row.due_date != due:
        row.due_date = due
        changed.append("due_date")
    if row.status == PlannedVisitStatus.PLANNED and row.technician_id is None and default:
        row.technician_id = default
        changed.append("technician")
    return changed
