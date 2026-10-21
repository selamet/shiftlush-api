"""The contract lifecycle, run daily (core/scheduling.py).

M2: docs/superpowers/specs/2026-10-20-m2-notifications-design.md. Both jobs run
across every company under the system context, and both are safe to run twice:
`expire()` does nothing to a contract it has already closed, and every
notification carries a dedupe key.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from django.tasks import task

from apps.contracts.models import Contract, ContractStatus
from apps.contracts.services import expire
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User
from core.context import system_context
from core.scheduling import local_today

logger = logging.getLogger(__name__)

# The longest notice anyone could reasonably ask for. Narrowing the query by it
# keeps the job from reading every active contract in the country each morning;
# the exact per-contract window is applied below.
_LONGEST_NOTICE = timedelta(days=366)


def _contract_people(company_id: Any) -> list[User]:
    """Who hears about a contract: the people who can act on one."""
    return list(
        User.objects.filter(
            company_id=company_id, role__in=[Role.OWNER, Role.ADMIN], is_active=True
        )
    )


def _facts(contract: Contract) -> dict[str, Any]:
    return {
        "contract_number": contract.contract_number,
        "customer_name": contract.customer.legal_name,
        "end_date": contract.end_date.isoformat(),
        "auto_renew": contract.auto_renew,
    }


@task
def contract_renewal_notice() -> int:
    """Warn owners and admins `renewal_notice_days` before a contract ends.

    Once per term: the dedupe key carries the end date, so a contract renewed
    onto a new end date is warned about again, and one that is not is warned
    about exactly once however many mornings the window spans.
    """
    with system_context():
        return contract_renewal_notice_in_scope(today=local_today())


def contract_renewal_notice_in_scope(*, today: date) -> int:
    """`contract_renewal_notice`'s work, in whatever tenant scope the caller has open.

    The task runs it across every firm under the system context; the demo
    seeding (core.demo_activity) runs it for the demo company alone.
    """
    raised = 0
    candidates = (
        Contract.objects.filter(
            status=ContractStatus.ACTIVE,
            end_date__gt=today,
            end_date__lte=today + _LONGEST_NOTICE,
        )
        .select_related("customer")
        .order_by("end_date", "id")
    )
    for contract in candidates:
        if contract.end_date - timedelta(days=contract.renewal_notice_days) > today:
            continue
        raised += len(
            notify(
                kind=NotificationKind.CONTRACT_RENEWAL_DUE,
                recipients=_contract_people(contract.company_id),
                subject_type="contract",
                subject_id=contract.id,
                data=_facts(contract),
                dedupe_key=f"renewal_due:{contract.id}:{contract.end_date.isoformat()}",
            )
        )
    return raised


_OUTCOME_KIND = {
    "expired": NotificationKind.CONTRACT_EXPIRED,
    "auto_renewed": NotificationKind.CONTRACT_AUTO_RENEWED,
    "auto_renewal_blocked": NotificationKind.CONTRACT_AUTO_RENEWAL_BLOCKED,
}


@task
def contract_expiry() -> int:
    """Close every ACTIVE contract past its end date, and say what happened.

    One contract at a time, each in its own transaction (`expire` is atomic): a
    contract that cannot be closed — a renewal refused by a constraint nobody
    foresaw — is logged at ERROR, which reaches Sentry, and the rest still close.
    """
    with system_context():
        return contract_expiry_in_scope(today=local_today())


def contract_expiry_in_scope(*, today: date) -> int:
    """`contract_expiry`'s work, in whatever tenant scope the caller has open.

    The task runs it across every firm under the system context; the demo
    seeding (core.demo_activity) runs it for the demo company alone.
    """
    closed = 0
    overdue = (
        Contract.objects.filter(status=ContractStatus.ACTIVE, end_date__lt=today)
        .select_related("customer")
        .order_by("end_date", "id")
    )
    for contract in overdue:
        try:
            outcome = expire(contract, today=today)
        except Exception:
            logger.exception("Could not close contract %s", contract.id)
            continue
        if outcome is None:
            continue
        closed += 1
        # The link goes where the reader has something to do: the new
        # contract after a renewal, the old one after an expiry.
        subject = outcome.successor or contract
        data = _facts(contract)
        if outcome.successor is not None:
            data["successor_contract_number"] = outcome.successor.contract_number
        notify(
            kind=_OUTCOME_KIND[outcome.kind],
            recipients=_contract_people(contract.company_id),
            subject_type="contract",
            subject_id=subject.id,
            data=data,
            dedupe_key=f"{outcome.kind}:{contract.id}",
        )
    return closed
