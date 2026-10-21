"""Generating invoice drafts, and their lifecycle.

Generation is the daily `draft_invoices` job (apps.billing.tasks). Everything a
person does to a draft afterwards — prices, manual lines, notes, issue, paid,
cancel — goes through the functions below, which hold the two rules that make a
draft trustworthy: its totals are always recomputed from its lines while it is a
draft, and nothing about it moves once it has been issued.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.template.loader import render_to_string
from django.utils import timezone

from apps.billing import money
from apps.billing.models import InvoiceDraft, InvoiceLine, InvoiceLineKind, InvoiceStatus
from apps.billing.periods import Period, overlap_days, periods
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType
from apps.faults.models import Fault, FaultStatus
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.users.models import Role, User
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError, Conflict, DuplicateRecord
from core.scheduling import LOCAL_TZ

#: Contracts that ended (expired, or renewed into a successor) this recently are
#: still drafted, so their last period is billed the morning after it ends.
RECENTLY_ENDED = timedelta(days=92)
#: How far back the job drafts a period it finds missing: enough to survive the
#: scheduler being down for a while, short enough that deploying this module
#: onto years of contracts does not bury the accountant in historical drafts.
LOOKBACK = timedelta(days=92)

_BILLING_PEOPLE = (Role.OWNER, Role.ADMIN, Role.ACCOUNTANT)
_FAR_FUTURE = date.max


def _refuse() -> Conflict:
    return Conflict(ErrorCode.INVOICE_TRANSITION_NOT_ALLOWED)


def _describe(kind: str, **context: Any) -> str:
    # The words live in a template, like every other Turkish the backend writes.
    return render_to_string(f"billing/lines/{kind}.txt", context).strip()[:500]


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=LOCAL_TZ)


# Totals ----------------------------------------------------------------------


def recompute(draft: InvoiceDraft) -> InvoiceDraft:
    """Set the draft's totals from its lines. A no-op once it is not a draft."""
    if draft.status != InvoiceStatus.DRAFT:
        return draft
    amounts = InvoiceLine.objects.filter(draft=draft).values_list("amount", flat=True)
    draft.subtotal, draft.vat_amount, draft.total = money.totals(amounts, draft.vat_rate)
    draft.save(update_fields=["subtotal", "vat_amount", "total", "updated_at"])
    return draft


# Generation -------------------------------------------------------------------


@dataclass
class DraftRun:
    """What one run of the generator did."""

    created: list[InvoiceDraft] = field(default_factory=list)
    vat_missing: int = 0
    by_company: defaultdict[Any, list[InvoiceDraft]] = field(
        default_factory=lambda: defaultdict(list)
    )


def last_billed_day(contract: Contract) -> date:
    """The contract's last covered day: its end, or its termination if sooner.

    `terminate()` closes the contract's lines on `terminated_at`, and a line's
    removal day is a covered day (as for fault cover), so the termination date
    is billed and the day after it is not.
    """
    if contract.status == ContractStatus.TERMINATED and contract.terminated_at is not None:
        return min(contract.end_date, contract.terminated_at)
    return contract.end_date


def _prorated(
    kind: str, period: Period, covered: int, unit_price: Decimal | None, **context: Any
) -> InvoiceLine:
    """A fee line billing `covered` days of `period`, at a monthly `unit_price`."""
    return InvoiceLine(
        kind=kind,
        description=_describe(
            kind,
            period_start=period.start,
            period_end=period.end,
            prorated=covered < period.nominal_days,
            months=period.months,
            covered_days=covered,
            period_days=period.nominal_days,
            **context,
        ),
        quantity=money.prorated_quantity(period.months, covered, period.nominal_days),
        months=period.months,
        covered_days=covered,
        period_days=period.nominal_days,
        unit_price=unit_price,
        amount=money.prorated_amount(unit_price, period.months, covered, period.nominal_days),
        needs_price=unit_price is None,
    )


def _fee_lines(contract: Contract, period: Period) -> list[InvoiceLine]:
    if contract.pricing_type == PricingType.FLAT:
        covered = overlap_days(
            (period.start, period.end), (contract.start_date, last_billed_day(contract))
        )
        return [_prorated(InvoiceLineKind.CONTRACT_FEE, period, covered, contract.monthly_fee)]

    lines: list[InvoiceLine] = []
    contract_lines = (
        ContractElevator.objects.filter(contract=contract, added_at__lte=period.end)
        .filter(Q(removed_at__isnull=True) | Q(removed_at__gte=period.start))
        .select_related("elevator__building")
        .order_by("added_at", "id")
    )
    for line in contract_lines:
        # The day a lift is removed is still a covered day, as for fault cover.
        covered = overlap_days(
            (period.start, period.end), (line.added_at, line.removed_at or _FAR_FUTURE)
        )
        if covered == 0:
            continue
        fee = _prorated(
            InvoiceLineKind.ELEVATOR_FEE,
            period,
            covered,
            line.unit_price,
            elevator_name=line.elevator.name or line.elevator.registration_number,
            building_name=line.elevator.building.name,
        )
        fee.elevator = line.elevator
        lines.append(fee)
    return lines


def billable_faults(contract: Contract, period: Period) -> list[Fault]:
    """Faults to bill on this contract's draft for `period`.

    Closed within the period (Istanbul dates), billable, on a lift this contract
    covered on the day the fault was reported, and on no live draft already.
    """
    candidates = (
        Fault.objects.filter(
            status=FaultStatus.CLOSED,
            billable=True,
            closed_at__gte=_local_midnight(period.start),
            closed_at__lt=_local_midnight(period.end + timedelta(days=1)),
            elevator__contract_lines__contract=contract,
        )
        # One subquery rather than an exclude across the relation, which would
        # not require both conditions of the same line.
        .annotate(
            billed=Exists(
                InvoiceLine.objects.filter(
                    fault_id=OuterRef("pk"), fault_released=False, is_deleted=False
                )
            )
        )
        .filter(billed=False)
        .select_related("elevator__building")
        .distinct()
        .order_by("closed_at", "id")
    )
    cover = defaultdict(list)
    for line in ContractElevator.objects.filter(contract=contract):
        cover[line.elevator_id].append((line.added_at, line.removed_at or _FAR_FUTURE))
    chosen: list[Fault] = []
    for fault in candidates:
        reported_on = timezone.localtime(fault.reported_at, LOCAL_TZ).date()
        if any(start <= reported_on <= end for start, end in cover[fault.elevator_id]):
            chosen.append(fault)
    return chosen


def _fault_line(fault: Fault) -> InvoiceLine:
    return InvoiceLine(
        kind=InvoiceLineKind.FAULT,
        fault=fault,
        elevator=fault.elevator,
        description=_describe(
            InvoiceLineKind.FAULT,
            reference=fault.reference,
            elevator_name=fault.elevator.name or fault.elevator.registration_number,
            building_name=fault.elevator.building.name,
            parts_used=fault.parts_used.strip(),
        ),
        quantity=Decimal("1.0000"),
        unit_price=None,
        amount=money.ZERO,
        # A fault carries no price: the accountant enters one before issuing.
        needs_price=True,
    )


def draft_for(contract: Contract, period: Period) -> InvoiceDraft | None:
    """Create the draft for one period, with its lines, or None if it exists.

    Its own savepoint: a draft a concurrent run created first, or a fault
    another draft took a moment ago, skips this one rather than aborting the
    run on PostgreSQL.
    """
    assert contract.vat_rate is not None
    try:
        with transaction.atomic():
            draft = InvoiceDraft.objects.create(
                company_id=contract.company_id,
                contract=contract,
                customer_id=contract.customer_id,
                period_start=period.start,
                period_end=period.end,
                currency=contract.currency,
                vat_rate=contract.vat_rate,
            )
            lines = [*_fee_lines(contract, period)]
            lines += [_fault_line(fault) for fault in billable_faults(contract, period)]
            for position, line in enumerate(lines):
                line.company_id = contract.company_id
                line.draft = draft
                line.position = position
                line.save()
            recompute(draft)
    except IntegrityError:
        return None
    return draft


def _billing_people(company_id: Any) -> list[User]:
    return list(
        User.objects.filter(company_id=company_id, role__in=_BILLING_PEOPLE, is_active=True)
    )


@transaction.atomic
def generate(*, today: date) -> DraftRun:
    """Draft every period that ended yesterday or earlier and has no live draft.

    ACTIVE contracts, and contracts that expired, were renewed or were
    terminated in the last `RECENTLY_ENDED` — a terminated one up to its
    termination date. A contract with no VAT rate gets no draft: a total that
    silently left out the VAT would look complete and be wrong (SL-85), so the
    billing people are told instead, once per contract and period. Runs in the
    caller's tenant scope — the system context, from the job.
    """
    run = DraftRun()
    yesterday = today - timedelta(days=1)
    contracts = (
        Contract.objects.filter(
            Q(status=ContractStatus.ACTIVE)
            | Q(
                status__in=[ContractStatus.EXPIRED, ContractStatus.RENEWED],
                end_date__gte=today - RECENTLY_ENDED,
            )
            # Service delivered up to a termination is billed: the final,
            # partial period is cut at the termination date.
            | Q(status=ContractStatus.TERMINATED, terminated_at__gte=today - RECENTLY_ENDED)
        )
        .select_related("customer")
        .order_by("start_date", "id")
    )
    for contract in contracts:
        existing = set(
            InvoiceDraft.objects.filter(contract=contract)
            .exclude(status=InvoiceStatus.CANCELLED)
            .values_list("period_start", flat=True)
        )
        for period in periods(
            start=contract.start_date,
            end=last_billed_day(contract),
            billing_period=contract.billing_period,
        ):
            if period.end > yesterday:
                break
            if period.end < today - LOOKBACK or period.start in existing:
                continue
            if contract.vat_rate is None:
                run.vat_missing += len(
                    notify(
                        kind=NotificationKind.BILLING_VAT_MISSING,
                        recipients=_billing_people(contract.company_id),
                        subject_type="contract",
                        subject_id=contract.id,
                        data={
                            "contract_number": contract.contract_number,
                            "customer_name": contract.customer.legal_name,
                            "period_start": period.start.isoformat(),
                            "period_end": period.end.isoformat(),
                        },
                        dedupe_key=f"billing_vat_missing:{contract.id}:{period.start.isoformat()}",
                    )
                )
                continue
            draft = draft_for(contract, period)
            if draft is not None:
                run.created.append(draft)
                run.by_company[contract.company_id].append(draft)

    for company_id, drafts in run.by_company.items():
        unpriced = InvoiceDraft.objects.filter(
            pk__in=[d.pk for d in drafts], lines__needs_price=True, lines__is_deleted=False
        ).distinct()
        latest = max(d.period_end for d in drafts)
        notify(
            kind=NotificationKind.INVOICE_DRAFTS_READY,
            recipients=_billing_people(company_id),
            subject_type="billing",
            subject_id=company_id,
            data={
                "count": len(drafts),
                "needs_price": unpriced.count(),
                "period": f"{latest.year:04d}-{latest.month:02d}",
            },
            dedupe_key=f"invoice_drafts_ready:{company_id}:{today.isoformat()}",
        )
    return run


# Lifecycle --------------------------------------------------------------------


def line_amount(line: InvoiceLine) -> Decimal:
    """A line's amount from its exact value: the fraction if it is prorated."""
    if line.months is not None and line.covered_days is not None and line.period_days:
        return money.prorated_amount(
            line.unit_price, line.months, line.covered_days, line.period_days
        )
    return money.line_amount(line.quantity, line.unit_price)


def _locked(draft: InvoiceDraft) -> InvoiceDraft:
    return InvoiceDraft.objects.select_for_update(of=("self",)).get(pk=draft.pk)


def _editable(draft: InvoiceDraft) -> InvoiceDraft:
    draft = _locked(draft)
    if draft.status != InvoiceStatus.DRAFT:
        raise _refuse()
    return draft


@transaction.atomic
def edit_draft(draft: InvoiceDraft, *, changes: dict[str, Any], by: User) -> InvoiceDraft:
    draft = _editable(draft)
    for name, value in changes.items():
        setattr(draft, name, value)
    draft.updated_by = by
    draft.save()
    return draft


@transaction.atomic
def add_line(
    draft: InvoiceDraft,
    *,
    description: str,
    quantity: Decimal,
    unit_price: Decimal,
    by: User,
) -> InvoiceLine:
    """A manual line: anything the generator could not know about."""
    draft = _editable(draft)
    position = (
        InvoiceLine.objects.filter(draft=draft)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
        or 0
    )
    line = InvoiceLine.objects.create(
        company_id=draft.company_id,
        draft=draft,
        kind=InvoiceLineKind.MANUAL,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        amount=money.line_amount(quantity, unit_price),
        needs_price=False,
        position=position + 1,
        created_by=by,
        updated_by=by,
    )
    recompute(draft)
    return line


@transaction.atomic
def edit_line(line: InvoiceLine, *, changes: dict[str, Any], by: User) -> InvoiceLine:
    """Correct a line in a draft. A price entered settles `needs_price`.

    A prorated fee line keeps billing its exact fraction when only its price
    changes. A quantity the accountant types replaces the fraction: from then on
    the typed quantity is the exact one.
    """
    draft = _editable(line.draft)
    for name, value in changes.items():
        setattr(line, name, value)
    if "quantity" in changes:
        line.months = line.covered_days = line.period_days = None
    line.needs_price = line.unit_price is None
    line.amount = line_amount(line)
    line.updated_by = by
    line.save()
    recompute(draft)
    return line


@transaction.atomic
def remove_line(line: InvoiceLine, *, by: User) -> None:
    """Take a line off a draft. A fault line removed frees the fault."""
    draft = _editable(line.draft)
    line.updated_by = by
    line.save(update_fields=["updated_by", "updated_at"])
    line.delete()
    recompute(draft)


@transaction.atomic
def issue(
    draft: InvoiceDraft, *, invoice_number: str, invoice_date: date, by: User
) -> InvoiceDraft:
    """Record that the draft was issued in the e-invoicing system, and freeze it.

    Refused while any line waits for a price: an invoice with a zero for a
    repair nobody priced is the error this whole module exists to prevent.
    """
    draft = _editable(draft)
    if InvoiceLine.objects.filter(draft=draft, needs_price=True).exists():
        raise BusinessRuleError(ErrorCode.INVOICE_LINES_NEED_PRICE)
    taken = (
        InvoiceDraft.objects.filter(
            company_id=draft.company_id,
            invoice_number=invoice_number,
            status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PAID],
        )
        .exclude(pk=draft.pk)
        .exists()
    )
    if taken:
        raise DuplicateRecord(ErrorCode.DUPLICATE_INVOICE_NUMBER)
    recompute(draft)
    draft.status = InvoiceStatus.ISSUED
    draft.invoice_number = invoice_number
    draft.invoice_date = invoice_date
    draft.updated_by = by
    draft.save(
        update_fields=["status", "invoice_number", "invoice_date", "updated_by", "updated_at"]
    )
    return draft


@transaction.atomic
def mark_paid(draft: InvoiceDraft, *, paid_on: date, by: User) -> InvoiceDraft:
    draft = _locked(draft)
    if draft.status != InvoiceStatus.ISSUED:
        raise _refuse()
    draft.status = InvoiceStatus.PAID
    draft.paid_on = paid_on
    draft.updated_by = by
    draft.save(update_fields=["status", "paid_on", "updated_by", "updated_at"])
    return draft


@transaction.atomic
def cancel(draft: InvoiceDraft, *, reason: str, by: User) -> InvoiceDraft:
    """Cancel a draft or an issued draft, and give its faults back.

    The period is given back too (the unique constraint ignores cancelled
    drafts), so the next run drafts it again and picks the faults up. A
    cancelled issued draft stays as the record; the credit note is the
    e-invoicing system's business.
    """
    draft = _locked(draft)
    if draft.status not in (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED):
        raise _refuse()
    draft.status = InvoiceStatus.CANCELLED
    draft.cancelled_at = timezone.now()
    draft.cancel_reason = reason
    draft.updated_by = by
    draft.save(
        update_fields=["status", "cancelled_at", "cancel_reason", "updated_by", "updated_at"]
    )
    for line in InvoiceLine.objects.filter(draft=draft, fault__isnull=False, fault_released=False):
        line.fault_released = True
        line.save(update_fields=["fault_released", "updated_at"])
    return draft
