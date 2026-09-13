"""Business rules for a maintenance visit.

Two things live here rather than in the serializer: whether the named
contract actually backs this elevator (a question about the world, not
about the shape of the request), and keeping Elevator.next_maintenance_date
correct as visits are created, edited, and deleted - always recomputed from
the surviving rows, never incremented in place, so a backdated or deleted
visit cannot leave a stale date behind.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NotRequired, TypedDict

from django.db import transaction

from apps.contracts.models import Contract, ContractElevator, ContractStatus
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, MaintenanceVisitItem, VisitStatus
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError


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
