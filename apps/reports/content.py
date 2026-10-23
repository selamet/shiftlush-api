"""What one customer's month holds, read into plain data.

Design: docs/superpowers/specs/2026-10-22-monthly-customer-report-design.md,
"What the report contains". Nothing here renders: the PDF (and anything else
that wants the month) takes the dataclasses, so the rules about what counts
live in one place and the template stays a template.

Coverage. A lift is in a customer's report for the days a contract covers it:
a `ContractElevator` line whose `added_at`..`removed_at` overlaps the month, on
a contract that is not a draft and whose own dates overlap the month. Visits,
faults and inspections of a lift count only on those days, so a lift that left
the contract on the 15th does not answer for the 20th. A planned row that
ended the month still "planned" is a missed visit; one the generator
"cancelled" (it lost its cover) is not.

The reads are a fixed number of queries whatever the number of lifts: every
child table is read once, for all the lifts, and grouped here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Max, Q

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.contracts.models import ContractElevator, ContractStatus
from apps.customers.models import Customer
from apps.elevators.models import Elevator
from apps.faults.models import Fault, FaultPriority, FaultStatus
from apps.inspections.models import Inspection, NonConformity, NonConformityStatus
from apps.maintenance.api.v1.views import ROUTE_ORDER
from apps.maintenance.models import (
    ChecklistResult,
    MaintenanceVisit,
    PlannedVisit,
    PlannedVisitStatus,
    SignatureStatus,
    VisitStatus,
)
from apps.maintenance.services import month_bounds
from apps.maintenance.signatures import signature_state
from apps.portal.models import PortalAccount
from core.scheduling import LOCAL_TZ
from core.templatetags.email_format import tr_month

__all__ = [
    "ComingItem",
    "FaultLine",
    "InspectionLine",
    "Lead",
    "LiftSection",
    "ReportData",
    "ReportSummary",
    "VisitLine",
    "build_report",
    "month_bounds",
]

# A field named `date` shadows the type inside its own class body.
Day = date

#: A periodic inspection is "coming" when it is this near.
INSPECTION_HORIZON_DAYS = 60


@dataclass
class VisitLine:
    date: Day
    technician_first_name: str
    status: str
    #: (checklist item key, note) for each item marked "attention needed".
    attention: list[tuple[str, str]]
    parts: str
    signature_state: str
    signer_name: str
    signer_role: str
    signed_at: datetime | None
    amended: bool
    signature_attachment_id: str | None


@dataclass
class FaultLine:
    reference: str
    reported_at: datetime
    trapped: bool
    description: str
    #: The status at month end, not now.
    status: str
    response_minutes: int | None


@dataclass
class InspectionLine:
    date: Day
    label: str
    body: str
    #: (description, status) as they stood at month end.
    nonconformities: list[tuple[str, str]]
    #: Only while a correction is still open.
    correction_deadline: Day | None
    correction_open: bool


@dataclass
class LiftSection:
    name: str
    building: str
    registration: str
    visits: list[VisitLine] = field(default_factory=list)
    #: The last completed visit, when this month's planned row ended unfinished.
    missed: date | None = None
    #: True whenever the row ended unfinished, including a lift never visited
    #: (`missed` has no date to give then).
    was_missed: bool = False
    faults: list[FaultLine] = field(default_factory=list)
    inspections: list[InspectionLine] = field(default_factory=list)


@dataclass
class ComingItem:
    kind: str  # "maintenance", "inspection" or "correction"
    lift: str
    date: Day
    days_left: int | None


@dataclass
class Lead:
    #: "correction_passed", "missed", "emergencies" or "all_done".
    key: str
    count: int
    minutes: int | None = None


@dataclass
class ReportSummary:
    lifts: int
    visits_planned: int
    visits_completed: int
    visits_by_status: dict[str, int]
    signatures: dict[str, int]
    faults_opened: int
    faults_resolved: int
    faults_open_at_end: int
    emergencies: int
    emergency_response_minutes: int | None
    open_corrections: int

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportData:
    company_name: str
    logo_attachment_id: str | None
    customer_name: str
    period: str
    period_label: str
    generated_on: date
    summary: ReportSummary
    lead: Lead
    lifts: list[LiftSection]
    coming: list[ComingItem]
    firm_phone: str
    firm_email: str
    portal_url: str | None


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=LOCAL_TZ)


def _local_date(moment: datetime) -> date:
    return moment.astimezone(LOCAL_TZ).date()


def _minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _lift_name(lift: Elevator) -> str:
    return lift.name or lift.registration_number or str(lift.pk)


def _covered_days(start: date, end: date, customer: Customer) -> dict[Any, list[tuple[date, date]]]:
    """Each lift's covered day ranges, for lifts covered on some day of the month.

    The ranges are not cut at the month: a fault or inspection from before it
    still carries over when the lift was covered that day.
    """
    lines = (
        ContractElevator.objects.filter(
            contract__customer=customer,
            contract__start_date__lte=end,
            contract__end_date__gte=start,
            added_at__lte=end,
        )
        .filter(Q(removed_at__isnull=True) | Q(removed_at__gte=start))
        .exclude(contract__status=ContractStatus.DRAFT)
        .select_related("contract")
    )
    covered: dict[Any, list[tuple[date, date]]] = defaultdict(list)
    for line in lines:
        first = max(line.added_at, line.contract.start_date)
        last = min(line.removed_at or line.contract.end_date, line.contract.end_date)
        if first <= last:
            covered[line.elevator_id].append((first, last))
    return covered


def _is_covered(ranges: list[tuple[date, date]], day: date) -> bool:
    return any(first <= day <= last for first, last in ranges)


def _nonconformity_open_at(item: NonConformity, end: date) -> bool:
    if item.status == NonConformityStatus.OPEN:
        return True
    return item.status == NonConformityStatus.FIXED and bool(item.fixed_on and item.fixed_on > end)


def _nonconformity_status_at(item: NonConformity, end: date) -> str:
    return NonConformityStatus.OPEN if _nonconformity_open_at(item, end) else item.status


def _fault_status_at(fault: Fault, end: date) -> str:
    """The furthest the fault had got by the end of the month."""
    for status, moment in (
        (FaultStatus.CLOSED, fault.closed_at),
        (FaultStatus.RESOLVED, fault.resolved_at),
        (FaultStatus.ON_SITE, fault.on_site_at),
        (FaultStatus.ASSIGNED, fault.assigned_at),
    ):
        if moment is not None and _local_date(moment) <= end:
            return str(status)
    return str(FaultStatus.OPEN)


def _visit_lines(
    visits: list[MaintenanceVisit], signature_images: dict[Any, str]
) -> dict[Any, list[VisitLine]]:
    lines: dict[Any, list[VisitLine]] = defaultdict(list)
    for visit in visits:
        # Hand the state its answer so it does not ask the database per visit.
        visit.has_signature_image = visit.pk in signature_images  # type: ignore[attr-defined]
        lines[visit.elevator_id].append(
            VisitLine(
                date=visit.visit_date,
                technician_first_name=visit.technician.first_name,
                status=visit.status,
                attention=[
                    (item.item, item.note)
                    for item in visit.items.all()
                    if item.result == ChecklistResult.ATTENTION_NEEDED
                ],
                parts=visit.parts_replaced,
                signature_state=signature_state(visit),
                signer_name=visit.signer_name,
                signer_role=visit.signer_role,
                signed_at=visit.signed_at,
                amended=visit.amended_after_signature,
                signature_attachment_id=(
                    str(signature_images[visit.pk]) if visit.pk in signature_images else None
                ),
            )
        )
    return lines


def build_report(customer: Customer, period: str, *, today: date) -> ReportData:
    start, end = month_bounds(period)
    start_at, after_end_at = _day_start(start), _day_start(end + timedelta(days=1))
    company = customer.company

    covered = _covered_days(start, end, customer)
    lifts = list(
        Elevator.objects.filter(pk__in=covered)
        .select_related("building__neighborhood__district")
        .order_by(*(key.removeprefix("elevator__") for key in ROUTE_ORDER))
    )
    lift_ids = [lift.pk for lift in lifts]

    visits = list(
        MaintenanceVisit.objects.filter(
            elevator_id__in=lift_ids, visit_date__gte=start, visit_date__lte=end
        )
        .select_related("technician")
        .prefetch_related("items")
        .order_by("visit_date", "id")
    )
    visits = [v for v in visits if _is_covered(covered[v.elevator_id], v.visit_date)]
    signature_images: dict[Any, str] = {}
    for attachment in Attachment.unscoped.filter(
        company_id=company.pk,
        object_type=ObjectType.MAINTENANCE_VISIT,
        object_id__in=[v.pk for v in visits],
        category=AttachmentCategory.SIGNATURE,
        is_deleted=False,
    ):
        signature_images.setdefault(attachment.object_id, str(attachment.pk))
    visit_lines = _visit_lines(visits, signature_images)

    planned = {
        row.elevator_id: row
        for row in PlannedVisit.objects.filter(elevator_id__in=lift_ids, period=period)
    }
    missed_ids = [
        elevator_id
        for elevator_id, row in planned.items()
        if row.status == PlannedVisitStatus.PLANNED
    ]
    last_visits = {
        row["elevator_id"]: row["last"]
        for row in MaintenanceVisit.objects.filter(
            elevator_id__in=missed_ids, status=VisitStatus.COMPLETED, visit_date__lte=end
        )
        .order_by()
        .values("elevator_id")
        .annotate(last=Max("visit_date"))
    }

    faults = list(
        Fault.objects.filter(elevator_id__in=lift_ids, reported_at__lt=after_end_at)
        .exclude(status=FaultStatus.CANCELLED)
        .filter(
            Q(reported_at__gte=start_at)
            | Q(resolved_at__isnull=True)
            | Q(resolved_at__gte=start_at)
        )
        .order_by("reported_at", "id")
    )
    # A fault counts only if its lift was covered the day it was reported.
    faults = [f for f in faults if _is_covered(covered[f.elevator_id], _local_date(f.reported_at))]
    opened = [f for f in faults if f.reported_at >= start_at]
    resolved = [f for f in faults if f.resolved_at and start <= _local_date(f.resolved_at) <= end]
    open_at_end = [f for f in faults if not (f.resolved_at and _local_date(f.resolved_at) <= end)]
    fault_lines: dict[Any, list[FaultLine]] = defaultdict(list)
    response_by_fault: dict[Any, int | None] = {}
    for fault in opened:
        minutes = (
            _minutes_between(fault.reported_at, fault.on_site_at) if fault.on_site_at else None
        )
        response_by_fault[fault.pk] = minutes
        fault_lines[fault.elevator_id].append(
            FaultLine(
                reference=fault.reference,
                reported_at=fault.reported_at,
                trapped=fault.priority == FaultPriority.EMERGENCY,
                description=fault.description,
                status=_fault_status_at(fault, end),
                response_minutes=minutes,
            )
        )
    emergencies = [f for f in opened if f.priority == FaultPriority.EMERGENCY]
    responses = [
        response_by_fault[f.pk] for f in emergencies if response_by_fault[f.pk] is not None
    ]
    average_response = round(sum(responses) / len(responses)) if responses else None  # type: ignore[arg-type]

    inspections = list(
        Inspection.objects.filter(elevator_id__in=lift_ids, inspected_on__lte=end)
        .filter(Q(inspected_on__gte=start) | Q(correction_deadline__isnull=False))
        .prefetch_related("nonconformities")
        .order_by("inspected_on", "id")
    )
    inspection_lines: dict[Any, list[InspectionLine]] = defaultdict(list)
    open_corrections: list[tuple[Inspection, date]] = []
    for inspection in inspections:
        items = list(inspection.nonconformities.all())
        is_open = inspection.correction_deadline is not None and any(
            _nonconformity_open_at(item, end) for item in items
        )
        on_cover = _is_covered(covered[inspection.elevator_id], inspection.inspected_on)
        if is_open and on_cover and inspection.correction_deadline is not None:
            open_corrections.append((inspection, inspection.correction_deadline))
        if start <= inspection.inspected_on and on_cover:
            inspection_lines[inspection.elevator_id].append(
                InspectionLine(
                    date=inspection.inspected_on,
                    label=inspection.label,
                    body=inspection.inspection_body,
                    nonconformities=[
                        (item.description, _nonconformity_status_at(item, end)) for item in items
                    ],
                    correction_deadline=inspection.correction_deadline if is_open else None,
                    correction_open=is_open,
                )
            )

    sections = []
    for lift in lifts:
        row = planned.get(lift.pk)
        was_missed = row is not None and row.status == PlannedVisitStatus.PLANNED
        sections.append(
            LiftSection(
                name=_lift_name(lift),
                building=lift.building.name,
                registration=lift.registration_number,
                visits=visit_lines[lift.pk],
                missed=last_visits.get(lift.pk) if was_missed else None,
                was_missed=was_missed,
                faults=fault_lines[lift.pk],
                inspections=inspection_lines[lift.pk],
            )
        )

    names = {lift.pk: _lift_name(lift) for lift in lifts}
    coming = [
        ComingItem("maintenance", names[lift.pk], lift.next_maintenance_date, None)
        for lift in lifts
        if lift.next_maintenance_date
    ]
    coming += [
        ComingItem(
            "inspection",
            names[lift.pk],
            lift.next_inspection_date,
            (lift.next_inspection_date - today).days,
        )
        for lift in lifts
        if lift.next_inspection_date
        and (lift.next_inspection_date - today).days <= INSPECTION_HORIZON_DAYS
    ]
    coming += [
        ComingItem("correction", names[i.elevator_id], deadline, (deadline - today).days)
        for i, deadline in open_corrections
    ]
    coming.sort(key=lambda item: (item.date, item.kind, item.lift))

    by_status = defaultdict(int, dict.fromkeys(VisitStatus.values, 0))
    signatures = dict.fromkeys(SignatureStatus.values, 0)
    for visit in visits:
        by_status[visit.status] += 1
        if visit.signature_status:
            signatures[visit.signature_status] += 1
    counted_rows = [r for r in planned.values() if r.status != PlannedVisitStatus.CANCELLED]
    summary = ReportSummary(
        lifts=len(lifts),
        visits_planned=len(counted_rows),
        visits_completed=sum(1 for r in counted_rows if r.status == PlannedVisitStatus.DONE),
        visits_by_status=dict(by_status),
        signatures=signatures,
        faults_opened=len(opened),
        faults_resolved=len(resolved),
        faults_open_at_end=len(open_at_end),
        emergencies=len(emergencies),
        emergency_response_minutes=average_response,
        open_corrections=len(open_corrections),
    )

    passed = {i.elevator_id for i, deadline in open_corrections if deadline <= end}
    missed_count = sum(1 for section in sections if section.was_missed)
    if passed:
        lead = Lead("correction_passed", len(passed))
    elif missed_count:
        lead = Lead("missed", missed_count)
    elif emergencies:
        lead = Lead("emergencies", len(emergencies), average_response)
    else:
        lead = Lead("all_done", 0)

    has_portal = PortalAccount.objects.filter(customer=customer, is_active=True).exists()
    return ReportData(
        company_name=company.display_name,
        logo_attachment_id=str(company.logo_id) if company.logo_id else None,
        customer_name=customer.legal_name,
        period=period,
        period_label=tr_month(period),
        generated_on=today,
        summary=summary,
        lead=lead,
        lifts=sections,
        coming=coming,
        firm_phone=company.phone,
        firm_email=company.email,
        portal_url=f"{settings.FRONTEND_URL.rstrip('/')}/portal" if has_portal else None,
    )
