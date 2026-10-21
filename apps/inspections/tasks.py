"""Inspection warnings, run daily (core/scheduling.py).

M5: docs/superpowers/specs/2026-10-20-m5-periodic-inspection-design.md. Both
jobs run across every firm under the system context, and both are safe to run
twice: every notification is deduped per (inspection, kind, threshold).
"""

from __future__ import annotations

from datetime import date, timedelta

from django.tasks import task

from apps.contracts.models import ContractStatus
from apps.elevators.models import Elevator
from apps.inspections import services
from apps.inspections.models import Label
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from core.context import system_context
from core.scheduling import local_today

#: How far ahead an inspection is announced.
INSPECTION_NOTICE_DAYS = 30
#: Days before a yellow or red correction deadline when a warning goes out,
#: tightest last. A job that missed a morning sends the tightest threshold
#: already crossed, once, rather than every one it skipped.
CORRECTION_THRESHOLDS = (30, 14, 3)


@task
def inspection_due_notice() -> int:
    """Tell the office 30 days before a lift's next periodic inspection.

    Lifts an ACTIVE contract covers: the firm books the inspections of the lifts
    it maintains. Keyed by the lift and the due date, which is what names one
    inspection cycle — a lift recorded since has a new date and a new notice.
    """
    with system_context():
        return inspection_due_notice_in_scope(today=local_today())


def inspection_due_notice_in_scope(*, today: date) -> int:
    """`inspection_due_notice`'s work, in whatever tenant scope the caller has open.

    The task runs it across every firm under the system context; the demo
    seeding (core.demo_activity) runs it for the demo company alone.
    """
    raised = 0
    due = (
        Elevator.objects.filter(
            next_inspection_date__gte=today,
            next_inspection_date__lte=today + timedelta(days=INSPECTION_NOTICE_DAYS),
            contract_lines__removed_at__isnull=True,
            contract_lines__is_deleted=False,
            contract_lines__contract__status=ContractStatus.ACTIVE,
        )
        .select_related("building")
        .distinct()
        .order_by("next_inspection_date", "id")
    )
    for elevator in due:
        assert elevator.next_inspection_date is not None
        raised += len(
            notify(
                kind=NotificationKind.INSPECTION_DUE,
                recipients=services.office(elevator.company_id),
                subject_type="elevator",
                subject_id=elevator.id,
                data={
                    "elevator_name": elevator.name,
                    "building_name": elevator.building.name,
                    "next_inspection_date": elevator.next_inspection_date.isoformat(),
                    "inspection_body": elevator.inspection_body,
                },
                dedupe_key=(
                    f"inspection_due:{elevator.id}:"
                    f"{elevator.next_inspection_date.isoformat()}:{INSPECTION_NOTICE_DAYS}"
                ),
            )
        )
    return raised


def threshold_for(days_left: int) -> int | None:
    """The tightest warning threshold `days_left` has crossed, if any."""
    crossed = [t for t in CORRECTION_THRESHOLDS if days_left <= t]
    return min(crossed) if crossed else None


@task
def correction_deadline_notice() -> int:
    """Warn before a yellow or red deadline, and say when it has passed.

    Only while defects are still open, and only for the lift's current
    inspection — a later result has replaced an older one's deadline. A passed
    deadline means the municipality may seal the lift, so it is one of the kinds
    that is e-mailed whatever the recipient's preference.
    """
    with system_context():
        return correction_deadline_notice_in_scope(today=local_today())


def correction_deadline_notice_in_scope(*, today: date) -> int:
    """`correction_deadline_notice`'s work, in whatever tenant scope the caller has open.

    The task runs it across every firm under the system context; the demo
    seeding (core.demo_activity) runs it for the demo company alone.
    """
    raised = 0
    candidates = (
        services.current_inspections()
        .filter(label__in=[Label.YELLOW, Label.RED], correction_deadline__isnull=False)
        .select_related("elevator__building", "follows")
        .order_by("correction_deadline", "id")
    )
    for inspection in candidates:
        assert inspection.correction_deadline is not None
        still_open = services.open_nonconformities(inspection).count()
        if not still_open:
            continue
        days_left = (inspection.correction_deadline - today).days
        if days_left < 0:
            kind = NotificationKind.CORRECTION_DEADLINE_PASSED
            threshold = "passed"
        else:
            crossed = threshold_for(days_left)
            if crossed is None:
                continue
            kind = NotificationKind.CORRECTION_DEADLINE_NEAR
            threshold = str(crossed)
        raised += len(
            notify(
                kind=kind,
                recipients=services.office_and_technicians(inspection.elevator),
                subject_type="elevator",
                subject_id=inspection.elevator_id,
                data={
                    **services.facts(inspection),
                    "days_left": days_left,
                    "open_nonconformities": still_open,
                },
                dedupe_key=f"{kind}:{inspection.pk}:{threshold}",
            )
        )
    return raised
