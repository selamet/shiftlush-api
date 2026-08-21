"""Contract state transitions.

Termination and renewal are business operations, not field edits. Doing them
through PATCH would push the side effects onto the client: closing every
elevator line, moving those elevators to "uncontracted", recording the reason
for the audit trail. A client that forgot one step would leave the data
inconsistent and nothing would say so.
"""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.contracts.models import Contract, ContractElevator, ContractStatus
from apps.elevators.models import Elevator, ElevatorStatus
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError


def next_contract_number(company_id: str) -> str:
    """`2026-0001`, per company, per year."""
    year = timezone.now().year
    prefix = f"{year}-"
    highest = (
        Contract.unscoped.filter(company_id=company_id, contract_number__startswith=prefix)
        .aggregate(highest=Max("contract_number"))
        .get("highest")
    )
    sequence = int(highest.split("-")[1]) + 1 if highest else 1
    return f"{prefix}{sequence:04d}"


@transaction.atomic
def add_elevators(
    *, contract: Contract, elevator_ids: list[str], unit_price: str | None = None
) -> int:
    """Put elevators under a contract.

    The database refuses a second open line for the same elevator, so the check
    here is about the answer the user gets, not about correctness: without it
    they would see an integrity error instead of being told which elevator is
    already covered.
    """
    added = 0
    for elevator_id in elevator_ids:
        elevator = Elevator.objects.filter(pk=elevator_id).first()
        if elevator is None:
            raise BusinessRuleError(ErrorCode.NOT_FOUND)

        if ContractElevator.objects.filter(elevator=elevator, removed_at__isnull=True).exists():
            raise BusinessRuleError(ErrorCode.ELEVATOR_ALREADY_CONTRACTED)

        ContractElevator.objects.create(
            company_id=contract.company_id,
            contract=contract,
            elevator=elevator,
            unit_price=unit_price,
            added_at=date.today(),
        )
        if elevator.status == ElevatorStatus.UNCONTRACTED:
            elevator.status = ElevatorStatus.ACTIVE
            elevator.save(update_fields=["status", "updated_at"])
        added += 1
    return added


@transaction.atomic
def remove_elevator(*, contract: Contract, elevator_id: str) -> None:
    """Close the line rather than deleting it — the billing history has to stay."""
    line = ContractElevator.objects.filter(
        contract=contract, elevator_id=elevator_id, removed_at__isnull=True
    ).first()
    if line is None:
        raise BusinessRuleError(ErrorCode.NOT_FOUND)

    line.removed_at = date.today()
    line.save(update_fields=["removed_at", "updated_at"])

    elevator = line.elevator
    elevator.status = ElevatorStatus.UNCONTRACTED
    elevator.save(update_fields=["status", "updated_at"])


@transaction.atomic
def terminate(*, contract: Contract, terminated_at: date, reason: str) -> Contract:
    """End a contract and everything hanging off it.

    Irreversible and it touches three tables, which is why the interface counts
    the consequences out loud before asking. The reason is mandatory because it
    is what the audit trail will be read for a year later.
    """
    if not reason.strip():
        raise BusinessRuleError(ErrorCode.TERMINATION_REASON_REQUIRED)
    if contract.status in (ContractStatus.TERMINATED, ContractStatus.RENEWED):
        raise BusinessRuleError(ErrorCode.VALIDATION_ERROR)

    open_lines = list(ContractElevator.objects.filter(contract=contract, removed_at__isnull=True))
    for line in open_lines:
        line.removed_at = terminated_at
        line.save(update_fields=["removed_at", "updated_at"])
        # Every covered elevator falls back to uncontracted. Leaving them
        # "active" would hide them from the list of what needs a contract.
        elevator = line.elevator
        elevator.status = ElevatorStatus.UNCONTRACTED
        elevator.save(update_fields=["status", "updated_at"])

    contract.status = ContractStatus.TERMINATED
    contract.terminated_at = terminated_at
    contract.termination_reason = reason
    contract.auto_renew = False
    contract.save(
        update_fields=[
            "status",
            "terminated_at",
            "termination_reason",
            "auto_renew",
            "updated_at",
        ]
    )
    return contract


@transaction.atomic
def renew(
    *,
    contract: Contract,
    start_date: date,
    end_date: date,
    carry_elevators: bool = True,
    copy_terms: bool = True,
) -> Contract:
    """Close a contract by superseding it.

    The result is a draft, which is what makes this reversible and why it needs
    none of termination's ceremony: nothing is billed until someone activates
    the new contract.
    """
    if contract.status in (ContractStatus.TERMINATED, ContractStatus.RENEWED):
        raise BusinessRuleError(ErrorCode.VALIDATION_ERROR)
    if end_date <= start_date:
        raise BusinessRuleError(ErrorCode.END_DATE_BEFORE_START_DATE)

    successor = Contract.objects.create(
        company_id=contract.company_id,
        customer=contract.customer,
        contract_number=next_contract_number(str(contract.company_id)),
        status=ContractStatus.DRAFT,
        scope=contract.scope,
        start_date=start_date,
        end_date=end_date,
        pricing_type=contract.pricing_type,
        monthly_fee=contract.monthly_fee if copy_terms else None,
        currency=contract.currency,
        vat_rate=contract.vat_rate if copy_terms else None,
        billing_period=contract.billing_period,
        auto_renew=contract.auto_renew,
        renewal_notice_days=contract.renewal_notice_days,
        previous_contract=contract,
    )

    if carry_elevators:
        for line in ContractElevator.objects.filter(contract=contract, removed_at__isnull=True):
            line.removed_at = start_date
            line.save(update_fields=["removed_at", "updated_at"])
            ContractElevator.objects.create(
                company_id=contract.company_id,
                contract=successor,
                elevator=line.elevator,
                unit_price=line.unit_price if copy_terms else None,
                added_at=start_date,
            )

    contract.status = ContractStatus.RENEWED
    contract.save(update_fields=["status", "updated_at"])
    return successor
