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

**Idempotent, per subject.** Each step adds only what is missing, judged per
lift or per scenario rather than per module — visit history for each lift with
no visits, an inspection for each lift with none, a fault for each state the
board lacks — so a stray hand-entered row fills its own slot and blocks nothing
else (SL-161: one hand-entered visit in the live demo once made the refresh skip
the history of every other lift). The generators it drives are idempotent
themselves. That is what lets `refresh_demo_data` bring the live
demo tenant up to date, and run again, without doubling anything.

Everything is relative to `today`, so the demo always looks current, and drawn
from a seeded generator, so two runs on the same day agree. The Turkish lives in
`core/data/demo.json`.
"""

from __future__ import annotations

import random
import struct
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
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
from apps.faults.models import Fault, FaultPriority, FaultStatus
from apps.inspections import services as inspections
from apps.inspections.models import Inspection, InspectionKind, Label, NonConformityStatus
from apps.maintenance import services as maintenance
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    PlannedVisit,
    PlannedVisitStatus,
    SignatureStatus,
    VisitStatus,
)
from apps.notifications.models import Notification
from apps.portal import services as portal
from apps.portal.models import PortalAccount
from apps.properties.models import Building
from apps.users.models import Role, User, UserCustomer
from core import mail
from core.context import company_context
from core.demo import DEMO, SEED
from core.scheduling import LOCAL_TZ, local_today

#: The customer contact's portal password. Meant to be known, like the staff
#: demo password: it is printed by the commands and written in DEPLOYMENT.md.
PORTAL_PASSWORD = "demo-portal-2026"

ACTIVITY = DEMO["activity"]

#: The middle of Erzurum, the demo firm's home province (demo.json "home"):
#: where its office is, and what its buildings are placed around.
DEMO_CENTRE = (39.9043, 41.2679)

#: How many lifts are left without a recent visit, so the overdue warnings, the
#: overdue rows of the plan and the M2 overdue notice all have subjects.
NEGLECTED_LIFTS = 3


@dataclass
class Activity:
    """What one run added; all zero on a run that found everything in place."""

    visits: int = 0
    planned_visits: int = 0
    planned_days: int = 0
    faults: int = 0
    inspections: int = 0
    portal_accounts: int = 0
    drafts: int = 0
    issued: int = 0
    notifications: int = 0
    positions: int = 0
    signatures: int = 0
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
        activity.positions += _positions(company)
        firm = _firm(company)
        _contract_lifecycle(firm, generator, day, activity)
        firm = _firm(company)  # an auto-renewal may have put more lifts under contract
        visits, offsets = _visit_history(firm, generator, day)
        activity.visits += visits
        activity.planned_visits += _plan(firm, generator, day, offsets, activity)
        activity.planned_days += _planned_days(day)
        activity.signatures += _signatures(firm, generator)
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
    """A full checklist, mostly OK; now and then a finding, noted on its own item.

    A note is only ever written under the item it is about — an intercom remark
    under "floor leveling accuracy" is the kind of slip that makes a demo stop
    being believed (audit P3-16) — so only items with a note in demo.json can
    come out "attention needed".
    """
    notes: dict[str, str] = ACTIVITY["attention_notes"]
    entries: list[maintenance.ChecklistEntry] = []
    for item in ChecklistItem.values:
        roll = rng.random()
        if roll < 0.08 and item in notes:
            entries.append(
                {"item": item, "result": ChecklistResult.ATTENTION_NEEDED, "note": notes[item]}
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


def _visit_history(
    firm: _Firm, rng: random.Random, today: date
) -> tuple[int, dict[Any, int | None]]:
    """Two months of monthly visits, each lift on its own day of the cycle.

    Every lift has an offset into a thirty-day cycle, and was visited that many
    days before today, thirty days before that, and so on. This step writes the
    two older rounds; the newest round is recorded after the plan exists, so it
    closes the plan's rows the way a visit recorded today does. The neglected
    lifts were last seen six weeks or more ago and are overdue. A handful of
    visits failed (incomplete, no access) and were finished three days later.

    Per lift, not per tenant: a lift with any visit of its own is left alone,
    and every other covered lift gets its history. A whole-module check was a
    trap — one visit typed by hand into the live demo tenant made the refresh
    skip the history of all sixty other lifts.

    Returns how many it wrote, and for each lift it seeded the offset of its
    newest round, or None for a neglected lift, which gets no newest round.
    """
    visited = set(MaintenanceVisit.objects.values_list("elevator_id", flat=True))
    fresh = [lift for lift in firm.covered if lift.pk not in visited]
    created = 0
    offsets: dict[Any, int | None] = {}
    for index, elevator in enumerate(fresh):
        offset = rng.randint(0, 29)
        neglected = index < NEGLECTED_LIFTS
        offsets[elevator.pk] = None if neglected else offset
        for months_back in (2, 1):
            day = today - timedelta(days=offset + 30 * months_back + (12 if neglected else 0))
            if rng.random() < 0.08:
                attempt = rng.choice([VisitStatus.INCOMPLETE, VisitStatus.ACCESS_DENIED])
                created += bool(_visit(firm, rng, elevator, day - timedelta(days=3), attempt))
            created += bool(_visit(firm, rng, elevator, day, VisitStatus.COMPLETED))
    return created, offsets


# The plan -----------------------------------------------------------------------------


def _plan(
    firm: _Firm,
    rng: random.Random,
    today: date,
    offsets: dict[Any, int | None],
    activity: Activity,
) -> int:
    """The newest round of visits, then this month's and next month's plan.

    Visits first and the plan after (audit P3-16): the generator then reads
    each lift's final next-maintenance date, so plan and lift agree from the
    start, and the month's visits close their rows through the same path a
    recorded visit takes. The neglected lifts get no visit and stay open and
    overdue; one lift's visit this month was only an attempt ("tried, not
    done"); two rows are handed back to the office unassigned.
    """
    before = PlannedVisit.objects.count()
    if not offsets:
        # Every lift already had its visits: nothing of this step is missing
        # beyond what the generator itself adds.
        maintenance.generate_plan(today=today)
        return PlannedVisit.objects.count() - before

    month_start = today.replace(day=1)
    by_pk = {lift.pk: lift for lift in firm.covered}
    recent = [(by_pk[pk], offset) for pk, offset in offsets.items() if offset is not None]
    for position, (elevator, newest) in enumerate(recent):
        tried = position == 0
        offset = min(newest, today.day - 1) if tried else newest
        status = VisitStatus.INCOMPLETE if tried else VisitStatus.COMPLETED
        if _visit(firm, rng, elevator, today - timedelta(days=offset), status):
            activity.visits += 1
    maintenance.generate_plan(today=today)
    # This month's visits were recorded before their rows existed (on a new
    # tenant), or against the daily job's rows (on the live one); either way
    # the closing path runs over them now that the rows are there.
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


#: How far ahead the demo office has decided its days: this week and the next.
PLANNED_DAYS_AHEAD = 14
#: A technician's day in the demo, well under the calendar's busy hint.
VISITS_PER_DAY = 4


def _planned_days(today: date) -> int:
    """Give the office's next two weeks their days, as an office would have.

    The calendar opens on this week; without days it is an empty grid and a
    full tray. Each technician's open rows, oldest due first, are spread over
    the working days (Monday to Saturday) from today to two weeks out, inside
    the row's own month, a few a day. About a third stay without a day, so the
    tray has something for the office to place. A period that already has a
    planned day is left alone: somebody has planned it, or an earlier run did.
    Returns how many rows got a day.
    """
    given = 0
    for period in (
        maintenance.period_of(today),
        maintenance.next_period(maintenance.period_of(today)),
    ):
        if PlannedVisit.objects.filter(period=period, planned_date__isnull=False).exists():
            continue
        days = [
            day
            for day in (today + timedelta(days=offset) for offset in range(PLANNED_DAYS_AHEAD))
            if day.weekday() != 6 and maintenance.period_of(day) == period
        ]
        if not days:
            continue
        rows = PlannedVisit.objects.filter(
            period=period,
            status=PlannedVisitStatus.PLANNED,
            technician__isnull=False,
            planned_date__isnull=True,
        ).order_by("technician_id", "due_date", "id")
        by_technician: dict[Any, list[PlannedVisit]] = {}
        for row in rows:
            by_technician.setdefault(row.technician_id, []).append(row)
        for own in by_technician.values():
            placed = own[: len(own) - len(own) // 3]
            for index, row in enumerate(placed[: len(days) * VISITS_PER_DAY]):
                # Round the days, so a week reads like a week, not a Monday.
                day = days[index % len(days)]
                PlannedVisit.objects.filter(pk=row.pk).update(planned_date=day)
                given += 1
    return given


# Signatures ----------------------------------------------------------------------------


def _png(width: int, height: int, ink: bytearray) -> bytes:
    """A greyscale PNG from one byte a pixel (255 white, 0 black), standard library only."""
    rows = b"".join(b"\x00" + bytes(ink[y * width : (y + 1) * width]) for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _signature_png(rng: random.Random) -> bytes:
    """A made-up signature: a few joined curves across a white strip.

    Drawn from the seeded generator, never from anybody's real hand — the
    demo must not carry a signature that could be mistaken for a person's.
    """
    width, height = 600, 240
    ink = bytearray(b"\xff" * (width * height))
    x, y = 60.0, height / 2 + rng.uniform(-30, 30)
    for _ in range(rng.randint(3, 5)):
        control = (x + rng.uniform(20, 90), y + rng.uniform(-80, 80))
        end = (min(x + rng.uniform(60, 140), width - 40), height / 2 + rng.uniform(-60, 60))
        for step in range(160):
            t = step / 159
            px = (1 - t) ** 2 * x + 2 * (1 - t) * t * control[0] + t**2 * end[0]
            py = (1 - t) ** 2 * y + 2 * (1 - t) * t * control[1] + t**2 * end[1]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cx, cy = int(px) + dx, int(py) + dy
                    if 0 <= cx < width and 0 <= cy < height:
                        ink[cy * width + cx] = 0
        x, y = end
    return _png(width, height, ink)


def _signatures(firm: _Firm, rng: random.Random) -> int:
    """Give the demo's completed visits the building's answer.

    Of every twenty, one declined, three with nobody there, the rest signed,
    so each state is on screen. A signed one carries a generated image; with no
    bucket to put it in (a laptop without MinIO) it says nobody was there
    instead, because a demo must never show a signature still on its way.
    Only visits with no answer yet are touched.
    """
    from apps.attachments.models import AttachmentCategory, ObjectType
    from apps.attachments.services import _backend_for, save_bytes
    from core import storage

    pending = list(
        MaintenanceVisit.objects.filter(status=VisitStatus.COMPLETED, signature_status="")
        .select_related("elevator")
        .order_by("visit_date", "id")
    )
    if not pending:
        return 0
    can_store = settings.DEMO_SIGNATURE_IMAGES and storage.reachable(
        _backend_for(AttachmentCategory.SIGNATURE)
    )
    signers = ACTIVITY["signers"]
    given = 0
    for index, visit in enumerate(pending):
        slot = index % 20
        status = (
            SignatureStatus.DECLINED
            if slot == 0
            else SignatureStatus.NOT_PRESENT
            if slot in (1, 2, 3) or not can_store
            else SignatureStatus.SIGNED
        )
        changes: dict[str, Any] = {"signature_status": status}
        if status == SignatureStatus.SIGNED:
            name, role = signers[index % len(signers)]
            moment = datetime.combine(
                visit.visit_date, time(rng.randint(10, 15), rng.randint(0, 59))
            )
            changes |= {
                "signer_name": name,
                "signer_role": role,
                "signed_at": moment.replace(tzinfo=LOCAL_TZ),
            }
            save_bytes(
                company_id=firm.company.id,
                object_type=ObjectType.MAINTENANCE_VISIT,
                object_id=visit.pk,
                category=AttachmentCategory.SIGNATURE,
                data=_signature_png(rng),
                mime_type="image/png",
                original_filename="imza.png",
                uploaded_by=visit.technician,
            )
        MaintenanceVisit.objects.filter(pk=visit.pk).update(**changes)
        given += 1
    return given


# Positions -----------------------------------------------------------------------------


def _offset(seed: Any, spread: float) -> tuple[float, float]:
    """A fixed nudge for one place, the same on every run."""
    rng = random.Random(str(seed))
    return (rng.uniform(-spread, spread), rng.uniform(-spread, spread) * 1.3)


def _positions(company: Company) -> int:
    """Put the firm and its buildings on the map, where the route order can read them.

    Not real addresses — the demo's are generated — but placed the way a city
    is: each district a few kilometres from the centre, each neighbourhood a
    cluster inside it, and each building a short walk from its neighbours, so
    a day's stops in one quarter are near each other and the suggested order
    has something to improve. Every offset is seeded by the place itself, so a
    rerun puts nothing anywhere new. Only what has no position is touched.
    """
    placed = 0
    if company.latitude is None or company.longitude is None:
        Company.objects.filter(pk=company.pk).update(
            latitude=round(DEMO_CENTRE[0], 6), longitude=round(DEMO_CENTRE[1], 6)
        )
        company.refresh_from_db(fields=["latitude", "longitude"])
    missing = Building.objects.filter(Q(latitude__isnull=True) | Q(longitude__isnull=True))
    for building in missing.select_related("neighborhood"):
        latitude, longitude = DEMO_CENTRE
        if building.neighborhood is not None:
            district = _offset(f"district:{building.neighborhood.district_id}", 0.03)
            quarter = _offset(f"neighborhood:{building.neighborhood_id}", 0.008)
            latitude += district[0] + quarter[0]
            longitude += district[1] + quarter[1]
        here = _offset(f"building:{building.pk}", 0.0015)
        Building.objects.filter(pk=building.pk).update(
            latitude=round(latitude + here[0], 6), longitude=round(longitude + here[1], 6)
        )
        placed += 1
    return placed


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
    """About ten faults, one in every state, through their real transitions.

    Per state, not per tenant: each scenario is seeded only if the tenant has
    no fault in that state yet, so a fault somebody entered by hand fills its
    own slot and blocks nothing else. Returns how many faults this added.
    """
    if len(firm.covered) < 8:
        return 0
    before = Fault.objects.count()
    open_states = [FaultStatus.OPEN, FaultStatus.ASSIGNED, FaultStatus.ON_SITE]
    missing = {
        "emergency": not Fault.objects.filter(
            priority=FaultPriority.EMERGENCY, status__in=open_states
        ).exists(),
        "open": not Fault.objects.filter(status=FaultStatus.OPEN)
        .exclude(priority=FaultPriority.EMERGENCY)
        .exists(),
        "assigned": not Fault.objects.filter(status=FaultStatus.ASSIGNED).exists(),
        "on_site": not Fault.objects.filter(status=FaultStatus.ON_SITE).exists(),
        "resolved": not Fault.objects.filter(status=FaultStatus.RESOLVED).exists(),
        "closed_billable": not Fault.objects.filter(
            status=FaultStatus.CLOSED, billable=True
        ).exists(),
        "closed_waived": not Fault.objects.filter(
            status=FaultStatus.CLOSED, billable=False
        ).exists(),
        "cancelled": not Fault.objects.filter(status=FaultStatus.CANCELLED).exists(),
    }
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

    if missing["emergency"]:
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

    if missing["open"]:
        opened = staff(lifts[1], text["open"])
        _shift(opened, reported_at=now - timedelta(hours=5))

    if missing["assigned"]:
        assigned = staff(lifts[2], text["assigned"], priority=FaultPriority.HIGH)
        faults.assign(assigned, technician=_technician_for(firm, lifts[2]), by=office)
        _shift(assigned, reported_at=now - timedelta(hours=3), assigned_at=now - timedelta(hours=2))

    if missing["on_site"]:
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

    if missing["resolved"]:
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

    if missing["closed_billable"]:
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

    if missing["closed_waived"]:
        # Closed and waived: the office overrode the derived "billable".
        free_lift = lifts[6]
        waived = staff(free_lift, text["closed_free"])
        technician = _technician_for(firm, free_lift)
        faults.assign(waived, technician=technician, by=office)
        faults.arrive(waived, by=technician)
        faults.resolve(
            waived, by=technician, resolution_notes=text["closed_free_notes"], parts_used=""
        )
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

    if missing["cancelled"]:
        duplicate = staff(lifts[1], text["duplicate"])
        faults.cancel(duplicate, reason=text["duplicate_reason"], by=office)
        _shift(duplicate, reported_at=now - timedelta(hours=4, minutes=50))

    return Fault.objects.count() - before


# Inspections ----------------------------------------------------------------------------


def _inspections(firm: _Firm, rng: random.Random, today: date) -> int:
    """A latest periodic inspection for every lift, and the few that matter.

    Mostly green and blue. Two yellow with open defects and their deadline a few
    weeks away; one red, which stops its lift, with a defect a fault has been
    opened for; one lift whose last year went yellow and was cleared by a
    follow-up; and two whose next inspection falls within the month.

    Per lift and per scenario, not per tenant: only lifts with no inspection of
    their own are inspected here, and each of the scenarios above is seeded only
    if the tenant does not already show one. Returns how many this added.
    """
    before = Inspection.objects.count()
    text = ACTIVITY["nonconformities"]
    bodies = ACTIVITY["inspection_bodies"]
    already = set(Inspection.objects.values_list("elevator_id", flat=True))
    lifts = [
        lift
        for lift in Elevator.objects.select_related("building").order_by(
            "registration_number", "id"
        )
        if lift.pk not in already
    ]
    if not lifts:
        return 0
    covered = [lift for lift in firm.covered if lift in lifts]
    open_yellow = (
        Inspection.objects.filter(
            label=Label.YELLOW,
            correction_deadline__gt=today,
            nonconformities__status=NonConformityStatus.OPEN,
        )
        .values("elevator")
        .distinct()
        .count()
    )
    wanted = {
        "yellow": max(0, 2 - open_yellow),
        "red": 0 if Inspection.objects.filter(label=Label.RED).exists() else 1,
        "follow_up": 0 if Inspection.objects.filter(kind=InspectionKind.FOLLOW_UP).exists() else 1,
        "due": 0
        if Inspection.objects.filter(
            next_inspection_due__gte=today, next_inspection_due__lte=today + timedelta(days=30)
        ).exists()
        else 2,
    }
    special: dict[str, list[Elevator]] = {}
    pool = list(reversed(covered)) if len(covered) >= 6 else []
    for scenario in ("yellow", "red", "follow_up", "due"):
        special[scenario], pool = pool[: wanted[scenario]], pool[wanted[scenario] :]
    number = before

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
    return Inspection.objects.count() - before


# Portal ----------------------------------------------------------------------------------


def _portal(firm: _Firm, staff_domain: str) -> int:
    """One contact signed up to the portal, and one still invited.

    Each only if the tenant has none of its kind yet, and never for a contact
    that already has an account: an account somebody made by hand fills its
    own slot rather than blocking the other. Returns how many this added.
    """
    before = PortalAccount.objects.count()
    want_active = not PortalAccount.objects.filter(is_active=True).exists()
    want_invited = not PortalAccount.objects.filter(
        is_active=False, accepted_at__isnull=True, deactivated_at__isnull=True
    ).exists()
    if not (want_active or want_invited):
        return 0
    taken = set(PortalAccount.objects.values_list("contact_id", flat=True))
    customers = list(
        Customer.objects.filter(contracts__status=ContractStatus.ACTIVE, is_active=True)
        .distinct()
        .order_by("legal_name", "id")
    )
    if len(customers) < 2:
        return 0
    names = ACTIVITY["portal"]
    for customer, local, accept, wanted in (
        (customers[0], names["active_email_local"], True, want_active),
        (customers[1], names["pending_email_local"], False, want_invited),
    ):
        if not wanted:
            continue
        contact = (
            CustomerContact.objects.filter(customer=customer)
            .exclude(pk__in=taken)
            .order_by("-is_primary", "id")
            .first()
        )
        if contact is None:
            continue
        if not contact.email:
            contact.email = f"{local}@{staff_domain}"
            contact.save(update_fields=["email", "updated_at"])
        _account, token = portal.invite(customer=customer, contact=contact)
        if accept:
            portal.accept(token=token, password=PORTAL_PASSWORD)
    return PortalAccount.objects.count() - before


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
    # Two slots, filled separately: some drafts issued, and one of them paid.
    # A draft somebody issued by hand fills the first and does not stop the
    # second.
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
        used = set(InvoiceDraft.objects.values_list("invoice_number", flat=True))
        serial = 0
        for draft in candidates:
            serial += 1
            while f"DMO{today.year}{serial:09d}" in used:
                serial += 1
            billing.issue(
                draft,
                invoice_number=f"DMO{today.year}{serial:09d}",
                invoice_date=min(today, draft.period_end + timedelta(days=3)),
                by=firm.owner,
            )
            issued += 1
    if not InvoiceDraft.objects.filter(status=InvoiceStatus.PAID).exists():
        oldest = (
            InvoiceDraft.objects.filter(status=InvoiceStatus.ISSUED)
            .order_by("period_end", "id")
            .first()
        )
        if oldest is not None and oldest.invoice_date is not None:
            billing.mark_paid(
                oldest,
                paid_on=min(
                    today, max(oldest.invoice_date, oldest.period_end + timedelta(days=12))
                ),
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
