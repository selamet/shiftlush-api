"""Maintenance jobs, run daily (core/scheduling.py).

M2: docs/superpowers/specs/2026-10-20-m2-notifications-design.md (overdue).
M3: docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md (plan).
"""

from __future__ import annotations

from django.tasks import task

from apps.contracts.models import ContractStatus
from apps.elevators.models import Elevator
from apps.maintenance.services import generate_plan
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User
from core.context import system_context
from core.scheduling import local_today


@task
def maintenance_overdue() -> int:
    """Tell the office, and the technicians who cover it, when a lift is overdue.

    Only lifts an ACTIVE contract covers: an uncontracted lift has no visit
    anyone owes it. Once per missed due date — the dedupe key carries the date,
    so recording a visit moves it and a later miss is a new notice, while the
    same miss is not repeated every morning until somebody gets there.
    """
    today = local_today()
    raised = 0
    with system_context():
        overdue = (
            Elevator.objects.filter(
                next_maintenance_date__lt=today,
                contract_lines__removed_at__isnull=True,
                contract_lines__contract__status=ContractStatus.ACTIVE,
            )
            .select_related("building")
            .distinct()
            .order_by("next_maintenance_date", "id")
        )
        for elevator in overdue:
            assert elevator.next_maintenance_date is not None
            office = User.objects.filter(
                company_id=elevator.company_id,
                role__in=[Role.OWNER, Role.ADMIN, Role.OPERATIONS],
                is_active=True,
            )
            technicians = User.objects.filter(
                company_id=elevator.company_id,
                role=Role.TECHNICIAN,
                is_active=True,
                customer_assignments__customer_id=elevator.building.customer_id,
                customer_assignments__is_deleted=False,
            )
            raised += len(
                notify(
                    kind=NotificationKind.MAINTENANCE_OVERDUE,
                    recipients=[*office, *technicians],
                    subject_type="elevator",
                    subject_id=elevator.id,
                    data={
                        "elevator_name": elevator.name,
                        "building_name": elevator.building.name,
                        "next_maintenance_date": elevator.next_maintenance_date.isoformat(),
                    },
                    dedupe_key=(
                        f"overdue:{elevator.id}:{elevator.next_maintenance_date.isoformat()}"
                    ),
                )
            )
    return raised


@task
def plan_maintenance() -> dict[str, int]:
    """Keep this month's and next month's maintenance plan complete (M3).

    docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md. Across
    every firm, under the system context, like the other daily jobs: there is
    no request to take a tenant from, and every row it writes carries its
    lift's company. Idempotent through the unique (elevator, period).
    """
    today = local_today()
    with system_context():
        run = generate_plan(today=today)
    return {
        "created": run.created,
        "updated": run.updated,
        "cancelled": run.cancelled,
        "notified": run.notified,
    }
