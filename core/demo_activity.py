"""The demo company's Phase 2 life: visits, the plan, faults, inspections,
the portal, invoice drafts and the notifications all of that raises.

`core.demo` builds the firm — staff, customers, buildings, lifts, contracts.
This module makes it look like the firm has been working for a few months, so a
demonstration opens on screens that are full and current rather than empty.

Three rules shape it:

- **The real services, not hand-made rows**, wherever a module has one: the plan
  is the plan generator's, faults go through their transitions, inspections
  through `record()`, drafts through the billing generator, and the daily jobs'
  own scoped functions raise their notifications. Demo data that the product
  could not have produced is a demonstration of something else. Visits are the
  exception — they are created in the past, which no service does — and they
  then go through the same `recalculate_next_maintenance` and plan-closing path
  a recorded visit does.
- **One tenant only.** Everything runs inside the demo company's tenant
  context, and every generator used here works in whatever scope is open; none
  of the cross-company jobs is ever run.
- **No e-mail.** The whole run is inside `core.mail.suppressed()`: in-app
  notifications are raised, nothing is sent, and the demo owner's address is a
  real mailbox.

**Idempotent.** Each step checks whether its module already holds data in this
tenant and adds only what is missing, and the generators it drives are
idempotent themselves. That is what lets `refresh_demo_data` bring the live
demo tenant up to date, and run again, without doubling anything.

Everything is relative to `today`, so the demo always looks current, and drawn
from a seeded generator, so two runs on the same day agree. The Turkish lives in
`core/data/demo.json`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import InvoiceDraft, InvoiceLine, InvoiceStatus
from apps.billing.periods import periods
from apps.companies.models import Company
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerContact
from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults import services as faults
from apps.faults.models import Fault, FaultPriority
from apps.inspections import services as inspections
from apps.inspections.models import Inspection, InspectionKind, Label, NonConformityStatus
from apps.maintenance import services as maintenance
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.notifications.models import Notification
from apps.portal import services as portal
from apps.portal.models import PortalAccount
from apps.users.models import Role, User, UserCustomer
from core import mail
from core.context import company_context
from core.demo import DEMO, SEED
from core.scheduling import LOCAL_TZ, local_today

#: The customer contact's portal password. Meant to be known, like the staff
#: demo password: it is printed by the commands and written in DEPLOYMENT.md.
PORTAL_PASSWORD = "demo-portal-2026"

ACTIVITY = DEMO["activity"]

#: How many lifts are left without a recent visit, so the overdue warnings, the
#: overdue rows of the plan and the M2 overdue notice all have subjects.
NEGLECTED_LIFTS = 3


@dataclass
class Activity:
    """What one run added; all zero on a run that found everything in place."""

    visits: int = 0
    planned_visits: int = 0
    faults: int = 0
    inspections: int = 0
    portal_accounts: int = 0
    drafts: int = 0
    issued: int = 0
    notifications: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class _Firm:
    """The demo company's people and lifts, read once."""

    company: Company
    owner: User
    office: User
    technicians: list[User]
    technician_of: dict[Any, User]
    covered: list[Elevator]


def populate_activity(
    company: Company,
    *,
    staff_domain: str,
    today: date | None = None,
    rng: random.Random | None = None,
) -> Activity:
    """Give the demo company a few months of Phase 2 activity. Safe to run again."""
    day = today or local_today()
    generator = rng if rng is not None else random.Random(SEED + 2)
    activity = Activity()
    started = timezone.now()
    with mail.suppressed(), company_context(company.id), transaction.atomic():
        before = Notification.objects.count()
        firm = _firm(company)
        _contract_lifecycle(firm, generator, day, activity)
        firm = _firm(company)  # an auto-renewal may have put more lifts under contract
        visits, offsets = _visit_history(firm, generator, day)
        activity.visits += visits
        activity.planned_visits += _plan(firm, generator, day, offsets, activity)
        activity.faults += _faults(firm, generator, day)
        activity.inspections += _inspections(firm, generator, day)
        activity.portal_accounts += _portal(firm, staff_domain)
        drafts, issued = _billing(firm, day)
        activity.drafts += drafts
        activity.issued += issued
        _job_notices(day)
        _age_notifications(started, generator)
        activity.notifications = Notification.objects.count() - before
    return activity


# Who and what --------------------------------------------------------------------


def _firm(company: Company) -> _Firm:
    staff = list(User.objects.filter(company=company, is_active=True).order_by("created_at", "id"))
    owner = next(user for user in staff if user.role == Role.OWNER)
    office = next((user for user in staff if user.role == Role.OPERATIONS), owner)
    technicians = [user for user in staff if user.role == Role.TECHNICIAN]
    technician_of = {
        row.customer_id: row.user
        for row in UserCustomer.objects.select_related("user").order_by("assigned_at", "id")
        if row.user.is_active
    }
    covered = list(
        Elevator.objects.filter(
            contract_lines__removed_at__isnull=True,
            contract_lines__is_deleted=False,
            contract_lines__contract__status=ContractStatus.ACTIVE,
        )
        .select_related("building__customer")
        .distinct()
        .order_by("registration_number", "id")
    )
    return _Firm(company, owner, office, technicians, technician_of, covered)


def _technician_for(firm: _Firm, elevator: Elevator) -> User:
    return firm.technician_of.get(elevator.building.customer_id) or (
        firm.technicians[0] if firm.technicians else firm.office
    )


def _contract_on(elevator: Elevator, day: date) -> Contract | None:
    line = (
        ContractElevator.objects.filter(elevator=elevator, added_at__lte=day)
        .filter(Q(removed_at__isnull=True) | Q(removed_at__gte=day))
        .select_related("contract")
        .order_by("-added_at", "-id")
        .first()
    )
    if line is None:
        line = (
            ContractElevator.objects.filter(elevator=elevator, removed_at__isnull=True)
            .select_related("contract")
            .first()
        )
    return line.contract if line else None


def _at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=LOCAL_TZ)


# Contracts -------------------------------------------------------------------------


def _contract_lifecycle(firm: _Firm, rng: random.Random, today: date, activity: Activity) -> None:
    """One contract in its renewal window, and one auto-renewal that has happened."""
    from apps.contracts.services import next_contract_number
    from apps.contracts.tasks import contract_expiry_in_scope

    active = Contract.objects.filter(status=ContractStatus.ACTIVE).order_by("start_date", "id")
    window = active.filter(end_date__gt=today, end_date__lte=today + timedelta(days=60))
    if not window.exists():
        candidate = active.filter(start_date__lt=today - timedelta(days=30)).first()
        if candidate is not None:
            candidate.end_date = today + timedelta(days=45)
            candidate.save(update_fields=["end_date", "updated_at"])

    if Contract.objects.filter(status=ContractStatus.RENEWED).exists():
        return
    # A customer with lifts and no contract — the generator leaves the last few
    # without one. Its auto-renewing contract ended ten days ago; the real
    # expiry step renews and activates it, and says so.
    uncovered = (
        Elevator.objects.filter(building__customer__is_active=True)
        .exclude(contract_lines__removed_at__isnull=True, contract_lines__is_deleted=False)
        .select_related("building__customer")
        .order_by("building__customer__legal_name", "id")
    )
    lifts = list(uncovered)
    if not lifts:
        activity.notes.append("no uncontracted customer for the auto-renewal")
        return
    customer = lifts[0].building.customer
    mine = [lift for lift in lifts if lift.building.customer_id == customer.pk]
    start = today - timedelta(days=375)
    contract = Contract.objects.create(
        company=firm.company,
        customer=customer,
        contract_number=next_contract_number(firm.company.id),
        status=ContractStatus.ACTIVE,
        scope=Scope.MAINTENANCE_AND_REPAIR,
        start_date=start,
        end_date=today - timedelta(days=11),
        pricing_type=PricingType.PER_ELEVATOR,
        monthly_fee="2500.00",
        vat_rate="20.00",
        auto_renew=True,
    )
    for lift in mine:
        ContractElevator.objects.create(
            company=firm.company,
            contract=contract,
            elevator=lift,
            unit_price=f"{rng.randint(9, 14) * 100}.00",
            added_at=start,
        )
        if lift.status == ElevatorStatus.UNCONTRACTED:
            lift.status = ElevatorStatus.ACTIVE
            lift.save(update_fields=["status", "updated_at"])
    contract_expiry_in_scope(today=today)


# Maintenance visits ------------------------------------------------------------------


def _checklist(rng: random.Random) -> list[maintenance.ChecklistEntry]:
    entries: list[maintenance.ChecklistEntry] = []
    for item in ChecklistItem.values:
        roll = rng.random()
        if roll < 0.06:
            entries.append(
                {
                    "item": item,
                    "result": ChecklistResult.ATTENTION_NEEDED,
                    "note": rng.choice(ACTIVITY["attention_notes"]),
                }
            )
        elif roll < 0.09 and item == ChecklistItem.AUTOMATIC_RESCUE_DEVICE:
            entries.append({"item": item, "result": ChecklistResult.NOT_APPLICABLE, "note": ""})
        else:
            entries.append({"item": item, "result": ChecklistResult.OK, "note": ""})
    return entries


_STATUS_NOTES: dict[str, str] = {
    VisitStatus.INCOMPLETE: ACTIVITY["incomplete_note"],
    VisitStatus.ACCESS_DENIED: ACTIVITY["access_denied_note"],
}


def _visit(
    firm: _Firm, rng: random.Random, elevator: Elevator, day: date, status: str
) -> MaintenanceVisit | None:
    contract = _contract_on(elevator, day)
    if contract is None:
        return None
    visit = MaintenanceVisit.objects.create(
        company=firm.company,
        elevator=elevator,
        contract=contract,
        technician=_technician_for(firm, elevator),
        visit_date=day,
        status=status,
        status_note=_STATUS_NOTES.get(status, ""),
        work_notes=rng.choice(ACTIVITY["work_notes"]) if status == VisitStatus.COMPLETED else "",
        parts_replaced=rng.choice(ACTIVITY["parts"]) if rng.random() < 0.15 else "",
        created_by=firm.office,
        updated_by=firm.office,
    )
    if status == VisitStatus.COMPLETED:
        maintenance.set_checklist(visit=visit, items=_checklist(rng))
    maintenance.recalculate_next_maintenance(elevator)
    maintenance.sync_plan_for_visit(visit)
    return visit


def _visit_history(firm: _Firm, rng: random.Random, today: date) -> tuple[int, dict[Any, int]]:
    """Two months of monthly visits, each lift on its own day of the cycle.

    Every lift has an offset into a thirty-day cycle, and was visited that many
    days before today, thirty days before that, and so on. This step writes the
    two older rounds; the newest round is recorded after the plan exists, so it
    closes the plan's rows the way a visit recorded today does. The neglected
    lifts were last seen six weeks or more ago and are overdue. A handful of
    visits failed (incomplete, no access) and were finished three days later.

    Returns how many it wrote, and each lift's offset for the newest round.
    """
    if MaintenanceVisit.objects.exists():
        return 0, {}
    created = 0
    offsets: dict[Any, int] = {}
    for index, elevator in enumerate(firm.covered):
        offset = rng.randint(0, 29)
        offsets[elevator.pk] = offset
        neglected = index < NEGLECTED_LIFTS
        for months_back in (2, 1):
            day = today - timedelta(days=offset + 30 * months_back + (12 if neglected else 0))
            if rng.random() < 0.08:
                attempt = rng.choice([VisitStatus.INCOMPLETE, VisitStatus.ACCESS_DENIED])
                created += bool(_visit(firm, rng, elevator, day - timedelta(days=3), attempt))
            created += bool(_visit(firm, rng, elevator, day, VisitStatus.COMPLETED))
    return created, offsets


# The plan -----------------------------------------------------------------------------


def _plan(
    firm: _Firm, rng: random.Random, today: date, offsets: dict[Any, int], activity: Activity
) -> int:
    """This month's and next month's plan, then the newest round of visits.

    The rows are the real generator's. The newest visits then close them through
    the same path a recorded visit takes. The neglected lifts get no visit and
    stay open and overdue; one lift's visit this month was only an attempt
    ("tried, not done"); two rows are handed back to the office unassigned.
    """
    before = PlannedVisit.objects.count()
    maintenance.generate_plan(today=today)
    if not offsets:
        # The visits were already there: nothing of this step is missing. (The
        # rows may be the daily job's, on a tenant that has been running since
        # M3 — the newest visits below still close them.)
        return PlannedVisit.objects.count() - before

    month_start = today.replace(day=1)
    recent = [lift for index, lift in enumerate(firm.covered) if index >= NEGLECTED_LIFTS]
    for position, elevator in enumerate(recent):
        if elevator.pk not in offsets:
            continue
        tried = position == 0
        offset = min(offsets[elevator.pk], today.day - 1) if tried else offsets[elevator.pk]
        status = VisitStatus.INCOMPLETE if tried else VisitStatus.COMPLETED
        if _visit(firm, rng, elevator, today - timedelta(days=offset), status):
            activity.visits += 1
    # A visit from before this month started was recorded before its row
    # existed; nothing to close there, but this re-checks the month's rows the
    # way the closing path would have.
    for visit in MaintenanceVisit.objects.filter(visit_date__gte=month_start):
        maintenance.sync_plan_for_visit(visit)

    open_rows = PlannedVisit.objects.filter(
        period=maintenance.period_of(today),
        status=PlannedVisitStatus.PLANNED,
        technician__isnull=False,
        visit__isnull=True,
    )
    # Rows still to come, preferably: an unassigned row that is also overdue
    # is a different story from the one these two tell.
    upcoming = list(open_rows.filter(due_date__gte=today).order_by("due_date", "id")[:2])
    rest = list(open_rows.exclude(pk__in=[r.pk for r in upcoming]).order_by("-due_date", "id"))
    for row in (upcoming + rest)[:2]:
        maintenance.edit_planned_visit(row, changes={"technician": None}, editor=firm.office)
    return PlannedVisit.objects.count() - before


# Faults -------------------------------------------------------------------------------


def _shift(fault: Fault, **stamps: datetime | None) -> None:
    Fault.objects.filter(pk=fault.pk).update(**stamps)


def _ended_period_day(contract: Contract, today: date) -> tuple[date, date] | None:
    """Two days inside the contract's latest billing period that has ended."""
    ended = [
        period
        for period in periods(
            start=contract.start_date,
            end=billing.last_billed_day(contract),
            billing_period=contract.billing_period,
        )
        if period.end < today and period.end >= today - billing.LOOKBACK + timedelta(days=5)
    ]
    if not ended:
        return None
    last = ended[-1]
    reported = max(last.start, last.end - timedelta(days=6))
    return reported, min(last.end, reported + timedelta(days=2))


def _faults(firm: _Firm, rng: random.Random, today: date) -> int:
    """About ten faults, one in every state, through their real transitions."""
    if Fault.objects.exists() or len(firm.covered) < 8:
        return 0
    text = ACTIVITY["faults"]
    lifts = firm.covered[NEGLECTED_LIFTS:] + firm.covered[:NEGLECTED_LIFTS]
    now = timezone.now()
    office = firm.office

    def staff(elevator: Elevator, description: str, **extra: Any) -> Fault:
        return faults.report_by_staff(
            elevator=elevator,
            description=description,
            priority=extra.pop("priority", FaultPriority.NORMAL),
            reported_by=office,
            **extra,
        )

    # A trapped passenger, reported from the sticker an hour ago: the board's
    # emergency row, still open.
    emergency = faults.report_public(
        elevator=lifts[0],
        report=faults.PublicReport(
            description=text["emergency"],
            someone_trapped=True,
            elevator_stopped=True,
            reporter_name=text["emergency_reporter_name"],
            reporter_phone=text["emergency_reporter_phone"],
        ),
    )
    _shift(emergency, reported_at=now - timedelta(minutes=50))

    opened = staff(lifts[1], text["open"])
    _shift(opened, reported_at=now - timedelta(hours=5))

    assigned = staff(lifts[2], text["assigned"], priority=FaultPriority.HIGH)
    faults.assign(assigned, technician=_technician_for(firm, lifts[2]), by=office)
    _shift(assigned, reported_at=now - timedelta(hours=3), assigned_at=now - timedelta(hours=2))

    on_site = staff(lifts[3], text["on_site"])
    technician = _technician_for(firm, lifts[3])
    faults.assign(on_site, technician=technician, by=office)
    faults.arrive(on_site, by=technician)
    _shift(
        on_site,
        reported_at=now - timedelta(hours=4),
        assigned_at=now - timedelta(hours=3, minutes=30),
        on_site_at=now - timedelta(hours=2, minutes=40),
    )

    resolved = staff(lifts[4], text["resolved"])
    technician = _technician_for(firm, lifts[4])
    faults.assign(resolved, technician=technician, by=office)
    faults.arrive(resolved, by=technician)
    faults.resolve(resolved, by=technician, resolution_notes=text["resolved_notes"])
    _shift(
        resolved,
        reported_at=now - timedelta(days=1, hours=6),
        assigned_at=now - timedelta(days=1, hours=5),
        on_site_at=now - timedelta(days=1, hours=3),
        resolved_at=now - timedelta(days=1, hours=1),
    )

    # Closed and billable, inside a billing period that has ended, so the
    # invoice draft for that period carries it as a line waiting for a price.
    billable_lift, days = None, None
    for candidate in lifts[5:]:
        contract = _contract_on(candidate, today)
        days = _ended_period_day(contract, today) if contract else None
        if days:
            billable_lift = candidate
            break
    if billable_lift is not None and days is not None:
        reported_on, closed_on = days
        repaired = staff(billable_lift, text["closed_billable"])
        _shift(repaired, reported_at=_at(reported_on, 9, 15))
        technician = _technician_for(firm, billable_lift)
        faults.assign(repaired, technician=technician, by=office)
        faults.arrive(repaired, by=technician)
        faults.resolve(
            repaired,
            by=technician,
            resolution_notes=text["closed_billable_notes"],
            parts_used=ACTIVITY["parts"][0],
            restore_service=True,
        )
        faults.close(repaired, by=office)
        _shift(
            repaired,
            assigned_at=_at(reported_on, 9, 40),
            on_site_at=_at(reported_on, 11, 5),
            resolved_at=_at(reported_on, 13, 30),
            closed_at=_at(closed_on, 10, 0),
        )

    # Closed and waived: the office overrode the derived "billable".
    free_lift = lifts[6]
    waived = staff(free_lift, text["closed_free"])
    technician = _technician_for(firm, free_lift)
    faults.assign(waived, technician=technician, by=office)
    faults.arrive(waived, by=technician)
    faults.resolve(waived, by=technician, resolution_notes=text["closed_free_notes"], parts_used="")
    faults.edit(waived, changes={"billable": False}, by=office)
    faults.close(waived, by=office)
    _shift(
        waived,
        reported_at=now - timedelta(days=3, hours=4),
        assigned_at=now - timedelta(days=3, hours=3),
        on_site_at=now - timedelta(days=3, hours=1),
        resolved_at=now - timedelta(days=3),
        closed_at=now - timedelta(days=2),
    )

    duplicate = staff(lifts[1], text["duplicate"])
    faults.cancel(duplicate, reason=text["duplicate_reason"], by=office)
    _shift(duplicate, reported_at=now - timedelta(hours=4, minutes=50))
    return Fault.objects.count()


# Inspections ----------------------------------------------------------------------------


def _inspections(firm: _Firm, rng: random.Random, today: date) -> int:
    """A latest periodic inspection for every lift, and the few that matter.

    Mostly green and blue. Two yellow with open defects and their deadline a few
    weeks away; one red, which stops its lift, with a defect a fault has been
    opened for; one lift whose last year went yellow and was cleared by a
    follow-up; and two whose next inspection falls within the month.
    """
    if Inspection.objects.exists():
        return 0
    text = ACTIVITY["nonconformities"]
    bodies = ACTIVITY["inspection_bodies"]
    lifts = list(Elevator.objects.select_related("building").order_by("registration_number", "id"))
    covered = [lift for lift in firm.covered if lift in lifts]
    special = {
        "yellow": covered[-2:] if len(covered) >= 6 else [],
        "red": covered[-3:-2] if len(covered) >= 6 else [],
        "follow_up": covered[-4:-3] if len(covered) >= 6 else [],
        "due": covered[-6:-4] if len(covered) >= 6 else [],
    }
    number = 0

    def record(elevator: Elevator, inspected_on: date, label: str, **extra: Any) -> Inspection:
        nonlocal number
        number += 1
        return inspections.record(
            elevator=elevator,
            inspected_on=inspected_on,
            label=label,
            by=firm.office,
            inspection_body=bodies[number % len(bodies)],
            report_number=f"PK-{inspected_on.year}-{number:05d}",
            **extra,
        )

    for elevator in lifts:
        if elevator in special["yellow"]:
            inspected = today - timedelta(days=120 - rng.randint(10, 25))
            record(
                elevator,
                inspected,
                Label.YELLOW,
                nonconformities=[
                    {"description": line, "severity": Label.YELLOW} for line in text["yellow"]
                ],
            )
        elif elevator in special["red"]:
            red = record(
                elevator,
                today - timedelta(days=rng.randint(5, 12)),
                Label.RED,
                nonconformities=[{"description": text["red"], "severity": Label.RED}],
            )
            inspections.open_fault(red.nonconformities.get(), by=firm.office)
        elif elevator in special["follow_up"]:
            last_year = record(
                elevator,
                today - timedelta(days=420),
                Label.YELLOW,
                nonconformities=[{"description": text["yellow"][0], "severity": Label.YELLOW}],
            )
            last_year.nonconformities.update(
                status=NonConformityStatus.FIXED, fixed_on=today - timedelta(days=335)
            )
            record(
                elevator,
                today - timedelta(days=330),
                Label.GREEN,
                kind=InspectionKind.FOLLOW_UP,
                follows=last_year,
            )
            record(elevator, today - timedelta(days=50), Label.GREEN)
        elif elevator in special["due"]:
            record(elevator, today - timedelta(days=345), Label.GREEN)
        else:
            label = Label.BLUE if rng.random() < 0.3 else Label.GREEN
            entries = (
                [{"description": text["blue"], "severity": Label.BLUE}]
                if label == Label.BLUE
                else []
            )
            record(
                elevator,
                today - timedelta(days=rng.randint(20, 320)),
                label,
                nonconformities=entries,
            )
    return Inspection.objects.count()


# Portal ----------------------------------------------------------------------------------


def _portal(firm: _Firm, staff_domain: str) -> int:
    """One contact signed up to the portal, and one still invited."""
    if PortalAccount.objects.exists():
        return 0
    customers = list(
        Customer.objects.filter(contracts__status=ContractStatus.ACTIVE, is_active=True)
        .distinct()
        .order_by("legal_name", "id")
    )
    if len(customers) < 2:
        return 0
    names = ACTIVITY["portal"]
    for customer, local, accept in (
        (customers[0], names["active_email_local"], True),
        (customers[1], names["pending_email_local"], False),
    ):
        contact = (
            CustomerContact.objects.filter(customer=customer).order_by("-is_primary", "id").first()
        )
        if contact is None:
            continue
        if not contact.email:
            contact.email = f"{local}@{staff_domain}"
            contact.save(update_fields=["email", "updated_at"])
        _account, token = portal.invite(customer=customer, contact=contact)
        if accept:
            portal.accept(token=token, password=PORTAL_PASSWORD)
    return PortalAccount.objects.count()


def portal_login(company: Company) -> str | None:
    """The demo portal account's e-mail, for the commands to print."""
    with company_context(company.id):
        account = PortalAccount.objects.filter(is_active=True).order_by("accepted_at", "id").first()
    return account.email if account else None


# Billing ----------------------------------------------------------------------------------


def _billing(firm: _Firm, today: date) -> tuple[int, int]:
    """The real draft generator, then the older drafts issued and one paid.

    On a tenant the daily job has been drafting since M7, the period a seeded
    fault was closed in may already have its draft. The generator's own fault
    selection then adds the fault to that draft, as a line waiting for a price,
    which is what the generator would have done had the fault existed in time.
    """
    run = billing.generate(today=today)
    for draft in InvoiceDraft.objects.filter(status=InvoiceStatus.DRAFT).select_related("contract"):
        period = next(
            (
                p
                for p in periods(
                    start=draft.contract.start_date,
                    end=billing.last_billed_day(draft.contract),
                    billing_period=draft.contract.billing_period,
                )
                if p.start == draft.period_start
            ),
            None,
        )
        if period is None:
            continue
        missing = billing.billable_faults(draft.contract, period)
        if not missing:
            continue
        position = InvoiceLine.objects.filter(draft=draft).count()
        for offset, fault in enumerate(missing):
            line = billing._fault_line(fault)
            line.company_id = draft.company_id
            line.draft = draft
            line.position = position + offset
            line.save()
        billing.recompute(draft)
    issued = 0
    if not InvoiceDraft.objects.filter(
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PAID]
    ).exists():
        priced = (
            InvoiceDraft.objects.filter(status=InvoiceStatus.DRAFT)
            .exclude(pk__in=InvoiceLine.objects.filter(needs_price=True).values("draft_id"))
            .order_by("period_end", "id")
        )
        candidates = list(priced.filter(period_end__lt=today - timedelta(days=20))) or list(
            priced[:1]
        )
        for index, draft in enumerate(candidates, start=1):
            invoice_date = min(today, draft.period_end + timedelta(days=3))
            billing.issue(
                draft,
                invoice_number=f"DMO{today.year}{index:09d}",
                invoice_date=invoice_date,
                by=firm.owner,
            )
            issued += 1
        if candidates:
            first = candidates[0]
            billing.mark_paid(
                first,
                paid_on=min(today, first.period_end + timedelta(days=12)),
                by=firm.owner,
            )
    return len(run.created), issued


# Notifications ----------------------------------------------------------------------------


def _job_notices(today: date) -> None:
    """What the daily jobs would have said, said for this tenant alone."""
    from apps.contracts.tasks import contract_renewal_notice_in_scope
    from apps.inspections.tasks import (
        correction_deadline_notice_in_scope,
        inspection_due_notice_in_scope,
    )
    from apps.maintenance.tasks import maintenance_overdue_in_scope

    maintenance_overdue_in_scope(today=today)
    contract_renewal_notice_in_scope(today=today)
    inspection_due_notice_in_scope(today=today)
    correction_deadline_notice_in_scope(today=today)


def _age_notifications(since: datetime, rng: random.Random) -> None:
    """Spread this run's notifications over the last week, the older ones read.

    Everything above was raised in the same second, which makes a bell that
    shows forty notifications all "just now". Only rows this run created are
    moved, so a second run leaves the first run's history alone.
    """
    now = timezone.now()
    fresh = Notification.objects.filter(created_at__gte=since).order_by("id")
    for notification in fresh:
        age = timedelta(minutes=rng.randint(5, 7 * 24 * 60))
        read = age > timedelta(days=1) and rng.random() < 0.7
        Notification.objects.filter(pk=notification.pk).update(
            created_at=now - age,
            read_at=(now - age + timedelta(minutes=rng.randint(5, 600))) if read else None,
        )
