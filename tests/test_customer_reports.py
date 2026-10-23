"""The monthly customer report: the record, and who it is meant for."""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core import mail as django_mail
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.address.models import District, Neighborhood, Province
from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.companies.models import Company
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerContact, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.faults.models import Fault, FaultChannel, FaultPriority, FaultStatus
from apps.inspections.models import (
    Inspection,
    Label,
    NonConformity,
    NonConformityStatus,
)
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.notifications.models import Notification, NotificationKind
from apps.portal import services as portal_services
from apps.properties.models import Building, BuildingType
from apps.reports import services
from apps.reports.content import (
    ComingItem,
    FaultLine,
    InspectionLine,
    Lead,
    LiftSection,
    ReportData,
    ReportSummary,
    VisitLine,
    build_report,
    month_bounds,
)
from apps.reports.documents import PdfRenderingUnavailable, build_html, render_pdf
from apps.reports.models import CustomerReport, ReportDelivery
from apps.reports.services import generate_one, regenerate, send_report
from apps.reports.tasks import monthly_customer_reports
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core import storage
from core.context import company_context, system_context
from core.exceptions import Conflict
from core.scheduling import LOCAL_TZ, get_schedules
from tests.identifiers import tax_number
from tests.test_attachments import FakeS3

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


@pytest.fixture
def firm(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company,
            type=CustomerType.CORPORATE,
            legal_name="Customer",
            tax_number=tax_number(1),
        )
    return company, owner, customer


def make_contact(company, customer, **fields) -> CustomerContact:
    with company_context(company.id):
        return CustomerContact.objects.create(
            company=company, customer=customer, full_name="Contact", **fields
        )


class TestTheRecord:
    def test_one_report_per_customer_and_month(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                CustomerReport.objects.create(
                    company=company, customer=customer, period="2026-09", summary={}
                )

    def test_a_new_report_waits_to_be_delivered(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            report = CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
        assert report.delivery == ReportDelivery.PENDING
        assert report.recipients == []

    def test_another_firms_reports_are_invisible(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
        other, _ = register_company(
            legal_name="Other Ltd",
            display_name="Other",
            first_name="O",
            last_name="Owner",
            email="other@example.com",
            password=PASSWORD,
        )
        with company_context(other.id):
            assert CustomerReport.objects.count() == 0

    def test_the_switch_is_off_and_owner_only(self, firm):
        company, owner, _ = firm
        with system_context():
            admin = User.objects.create_user(
                email="admin@example.com",
                password=PASSWORD,
                company=company,
                first_name="A",
                last_name="Admin",
                role=Role.ADMIN,
            )

        assert api_for(owner).get(reverse("company")).data["monthly_reports_enabled"] is False
        refused = api_for(admin).patch(
            reverse("company"), {"monthly_reports_enabled": True}, format="json"
        )
        assert refused.status_code == 403
        allowed = api_for(owner).patch(
            reverse("company"), {"monthly_reports_enabled": True}, format="json"
        )
        assert allowed.status_code == 200
        assert allowed.data["monthly_reports_enabled"] is True

    def test_a_contact_without_email_cannot_receive(self, firm):
        company, owner, customer = firm
        contact = make_contact(company, customer)
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "receives_monthly_report", "code": "MONTHLY_REPORT_NEEDS_EMAIL"}
        ]

    def test_a_contact_with_email_can_receive(self, firm):
        company, owner, customer = firm
        contact = make_contact(company, customer, email="a@example.com")
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["receives_monthly_report"] is True

    def test_clearing_the_email_unticks(self, firm):
        company, owner, customer = firm
        contact = make_contact(
            company, customer, email="a@example.com", receives_monthly_report=True
        )
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]), {"email": ""}, format="json"
        )
        assert response.status_code == 200, response.data
        assert response.data["receives_monthly_report"] is False
        with company_context(company.id):
            contact.refresh_from_db()
        assert contact.receives_monthly_report is False

    def test_asking_to_tick_while_clearing_the_email_is_refused(self, firm):
        company, owner, customer = firm
        contact = make_contact(
            company, customer, email="a@example.com", receives_monthly_report=True
        )
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"email": "", "receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "receives_monthly_report", "code": "MONTHLY_REPORT_NEEDS_EMAIL"}
        ]
        with company_context(company.id):
            contact.refresh_from_db()
        assert contact.email == "a@example.com"
        assert contact.receives_monthly_report is True

    def test_the_migration_ticks_the_primary_contact_with_email(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            other = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Other"
            )
        primary = make_contact(company, customer, email="p@example.com", is_primary=True)
        bare_primary = make_contact(company, other, is_primary=True)
        secondary = make_contact(company, customer, email="s@example.com")

        migration = importlib.import_module("apps.customers.migrations.0007_tick_primary_contacts")
        with system_context():
            migration.tick_primary_contacts(django_apps, None)
            ticked = {
                c.pk: c.receives_monthly_report
                for c in CustomerContact.objects.filter(
                    pk__in=[primary.pk, bare_primary.pk, secondary.pk]
                )
            }
        assert ticked == {primary.pk: True, bare_primary.pk: False, secondary.pk: False}


OCT, SEP = "2026-10", "2026-09"
TODAY = date(2026, 11, 1)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)


class Month:
    """A firm with two customers, and October 2026 and September 2026 to read.

    Customer A has lifts L1 and L2 (Kadikoy) and L3 (Besiktas), all on one
    contract for the whole year. Customer B has lift B1.
    """

    def __init__(self) -> None:
        self.company, self.owner = register_company(
            legal_name="Month Ltd",
            display_name="Month",
            first_name="M",
            last_name="Owner",
            email="owner-month@example.com",
            password=PASSWORD,
        )
        province, _ = Province.objects.get_or_create(
            id=34, defaults={"name": "Istanbul", "name_normalized": "istanbul"}
        )
        kadikoy, _ = District.objects.get_or_create(
            id=3401, defaults={"province": province, "name": "Kadikoy", "name_normalized": "k"}
        )
        besiktas, _ = District.objects.get_or_create(
            id=3402, defaults={"province": province, "name": "Besiktas", "name_normalized": "b"}
        )
        moda, _ = Neighborhood.objects.get_or_create(
            id=340101, defaults={"district": kadikoy, "name": "Moda", "name_normalized": "moda"}
        )
        levent, _ = Neighborhood.objects.get_or_create(
            id=340201, defaults={"district": besiktas, "name": "Levent", "name_normalized": "l"}
        )
        with system_context():
            self.tech = User.objects.create_user(
                email="tech-month@example.com",
                password=PASSWORD,
                company=self.company,
                first_name="Ali",
                last_name="Usta",
                role=Role.TECHNICIAN,
            )
        with company_context(self.company.id):
            self.a = Customer.objects.create(
                company=self.company,
                type=CustomerType.CORPORATE,
                legal_name="A Sitesi",
                tax_number=tax_number(11),
            )
            self.b = Customer.objects.create(
                company=self.company,
                type=CustomerType.CORPORATE,
                legal_name="B Plaza",
                tax_number=tax_number(12),
            )
            self.zeytin = self.building(self.a, "Zeytin", moda)
            self.cinar = self.building(self.a, "Cinar", levent)
            self.b_block = self.building(self.b, "B Blok", moda)
            self.contract_a = self.contract(self.a, "C-A")
            self.contract_b = self.contract(self.b, "C-B")
            # Created out of route order on purpose.
            self.l1 = self.lift(self.zeytin, "L1")
            self.l3 = self.lift(self.cinar, "L3")
            self.l2 = self.lift(self.zeytin, "L2")
            self.b1 = self.lift(self.b_block, "B1")
            for lift in (self.l1, self.l2, self.l3):
                self.cover(lift, self.contract_a)
            self.cover(self.b1, self.contract_b)

    def building(self, customer: Customer, name: str, neighborhood: Neighborhood) -> Building:
        return Building.objects.create(
            company=self.company,
            customer=customer,
            name=name,
            type=BuildingType.RESIDENTIAL,
            address_note="x",
            neighborhood=neighborhood,
        )

    def contract(self, customer: Customer, number: str, **fields: Any) -> Contract:
        values: dict[str, Any] = {
            "status": ContractStatus.ACTIVE,
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 12, 31),
        }
        values.update(fields)
        return Contract.objects.create(
            company=self.company,
            customer=customer,
            contract_number=number,
            scope=Scope.MAINTENANCE_ONLY,
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
            **values,
        )

    def lift(self, building: Building, name: str) -> Elevator:
        elevator = Elevator(
            company=self.company, building=building, name=name, registration_number=f"R-{name}"
        )
        assign_qr_token(elevator)
        elevator.save()
        return elevator

    def cover(
        self,
        lift: Elevator,
        contract: Contract,
        added: date = date(2026, 1, 1),
        removed: date | None = None,
    ) -> ContractElevator:
        return ContractElevator.objects.create(
            company=self.company,
            contract=contract,
            elevator=lift,
            added_at=added,
            removed_at=removed,
        )

    def visit(self, lift: Elevator, day: date, **fields: Any) -> MaintenanceVisit:
        line = lift.contract_lines.first()
        assert line is not None
        contract = line.contract
        return MaintenanceVisit.objects.create(
            company=self.company,
            elevator=lift,
            contract=contract,
            technician=self.tech,
            visit_date=day,
            status=fields.pop("status", VisitStatus.COMPLETED),
            **fields,
        )

    def planned(self, lift: Elevator, period: str, status: str) -> PlannedVisit:
        return PlannedVisit.objects.create(
            company=self.company,
            elevator=lift,
            period=period,
            due_date=date(int(period[:4]), int(period[5:]), 28),
            status=status,
        )

    def fault(self, lift: Elevator, reference: str, reported: datetime, **fields: Any) -> Fault:
        return Fault.objects.create(
            company=self.company,
            elevator=lift,
            reference=reference,
            channel=FaultChannel.STAFF,
            description="Door stuck",
            reported_at=reported,
            **fields,
        )

    def inspection(self, lift: Elevator, day: date, **fields: Any) -> Inspection:
        values: dict[str, Any] = {
            "label": Label.GREEN,
            "next_inspection_due": day + timedelta(days=365),
        }
        values.update(fields)
        return Inspection.objects.create(
            company=self.company, elevator=lift, inspected_on=day, **values
        )

    def report(self, customer: Customer | None = None, today: date = TODAY) -> Any:
        with company_context(self.company.id):
            return build_report(customer or self.a, OCT, today=today)


@pytest.fixture
def month(db) -> Month:
    return Month()


class TestContent:
    def test_month_bounds(self):
        assert month_bounds("2026-10") == (date(2026, 10, 1), date(2026, 10, 31))

    def test_the_header_names_the_firm_the_customer_and_the_month(self, month):
        report = month.report()
        assert report.customer_name == "A Sitesi"
        assert report.company_name == "Month"
        assert report.period_label == "Ekim 2026"
        assert report.generated_on == TODAY
        assert report.portal_url is None

    def test_only_lifts_covered_in_the_month(self, month):
        with company_context(month.company.id):
            gone = month.lift(month.zeytin, "GONE")
            month.cover(gone, month.contract_a, date(2026, 1, 1), date(2026, 9, 30))
            late = month.lift(month.zeytin, "LATE")
            month.cover(late, month.contract_a, date(2026, 10, 20))
            draft = month.lift(month.zeytin, "DRAFT")
            month.cover(draft, month.contract(month.a, "C-D", status=ContractStatus.DRAFT))
        names = {section.name for section in month.report().lifts}
        assert names == {"L1", "L2", "L3", "LATE"}
        assert month.report().summary.lifts == 4

    def test_visits_of_the_month_in_route_order_with_signatures(self, month):
        with company_context(month.company.id):
            visit = month.visit(
                month.l1,
                date(2026, 10, 8),
                parts_replaced="Door roller",
                signature_status="signed",
                signer_name="Veli Yilmaz",
                signer_role="manager",
                signed_at=at(date(2026, 10, 8), 11),
                amended_after_signature=True,
            )
            image = Attachment.objects.create(
                company=month.company,
                object_type=ObjectType.MAINTENANCE_VISIT,
                object_id=visit.id,
                category=AttachmentCategory.SIGNATURE,
                original_filename="imza.png",
                mime_type="image/png",
                size_bytes=1000,
                storage_key="k",
            )
            MaintenanceVisitItem.objects.create(
                company=month.company,
                visit=visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.ATTENTION_NEEDED,
                note="Worn pads",
            )
            MaintenanceVisitItem.objects.create(
                company=month.company,
                visit=visit,
                item=ChecklistItem.CAR_LIGHTING,
                result=ChecklistResult.OK,
            )
            month.visit(month.l1, date(2026, 9, 8))
        report = month.report()
        assert [section.name for section in report.lifts] == ["L3", "L1", "L2"]
        l1 = report.lifts[1]
        assert l1.building == "Zeytin"
        assert l1.registration == "R-L1"
        assert len(l1.visits) == 1
        line = l1.visits[0]
        assert line.date == date(2026, 10, 8)
        assert line.technician_first_name == "Ali"
        assert line.status == "completed"
        assert line.attention == [("brake_system", "Worn pads")]
        assert line.parts == "Door roller"
        assert line.signature_state == "signed"
        assert (line.signer_name, line.signer_role) == ("Veli Yilmaz", "manager")
        assert line.signed_at == at(date(2026, 10, 8), 11)
        assert line.amended is True
        assert line.signature_attachment_id == str(image.id)
        assert report.summary.visits_by_status["completed"] == 1
        assert report.summary.signatures == {"signed": 1, "not_present": 0, "declined": 0}

    def test_a_missed_visit_says_when_the_last_was(self, month):
        with company_context(month.company.id):
            month.visit(month.l1, date(2026, 9, 8))
            month.planned(month.l1, OCT, PlannedVisitStatus.PLANNED)
            month.planned(month.l2, OCT, PlannedVisitStatus.DONE)
        sections = {section.name: section for section in month.report().lifts}
        assert sections["L1"].missed == date(2026, 9, 8)
        assert sections["L1"].was_missed is True
        assert sections["L2"].missed is None
        assert sections["L2"].was_missed is False
        summary = month.report().summary
        assert (summary.visits_planned, summary.visits_completed) == (2, 1)

    def test_faults_with_response_minutes(self, month):
        with company_context(month.company.id):
            month.fault(
                month.l1,
                "AAAAA1",
                at(date(2026, 10, 5), 10),
                priority=FaultPriority.EMERGENCY,
                on_site_at=at(date(2026, 10, 5), 10, 42),
                resolved_at=at(date(2026, 10, 5), 12),
                status=FaultStatus.RESOLVED,
            )
            month.fault(
                month.l2,
                "AAAAA2",
                at(date(2026, 10, 6), 9),
                priority=FaultPriority.EMERGENCY,
                on_site_at=at(date(2026, 10, 6), 9, 20),
                status=FaultStatus.ON_SITE,
            )
            month.fault(month.l2, "AAAAA3", at(date(2026, 10, 7), 9))
            month.fault(month.l2, "AAAAA4", at(date(2026, 10, 8), 9), status=FaultStatus.CANCELLED)
            month.fault(month.l2, "AAAAA5", at(date(2026, 9, 8), 9))
        report = month.report()
        l1 = next(s for s in report.lifts if s.name == "L1")
        line = l1.faults[0]
        assert line.reference == "AAAAA1"
        assert line.response_minutes == 42
        assert line.trapped is True
        assert line.status == "resolved"
        summary = report.summary
        assert summary.faults_opened == 3
        assert summary.faults_resolved == 1
        # AAAAA2, AAAAA3 and the September one, still open on 31 October.
        assert summary.faults_open_at_end == 3
        assert summary.emergencies == 2
        assert summary.emergency_response_minutes == 31

    def test_inspections_and_open_corrections(self, month):
        with company_context(month.company.id):
            red = month.inspection(
                month.l1,
                date(2026, 10, 3),
                label=Label.RED,
                inspection_body="TSE",
                correction_deadline=date(2026, 11, 20),
            )
            NonConformity.objects.create(
                company=month.company, inspection=red, description="Worn rope", severity=Label.RED
            )
            NonConformity.objects.create(
                company=month.company,
                inspection=red,
                description="Light",
                severity=Label.YELLOW,
                status=NonConformityStatus.FIXED,
                fixed_on=date(2026, 10, 9),
            )
        report = month.report(today=date(2026, 11, 2))
        line = next(s for s in report.lifts if s.name == "L1").inspections[0]
        assert line.label == "red"
        assert line.body == "TSE"
        assert line.correction_open is True
        assert line.correction_deadline == date(2026, 11, 20)
        assert line.nonconformities == [("Worn rope", "open"), ("Light", "fixed")]
        assert report.summary.open_corrections == 1
        coming = [item for item in report.coming if item.kind == "correction"]
        assert [(c.lift, c.date, c.days_left) for c in coming] == [("L1", date(2026, 11, 20), 18)]

    def test_coming_lists_next_maintenance_and_near_inspections(self, month):
        today = date(2026, 11, 2)
        Elevator.unscoped.filter(pk=month.l1.pk).update(
            next_maintenance_date=date(2026, 11, 20),
            next_inspection_date=today + timedelta(days=50),
        )
        Elevator.unscoped.filter(pk=month.l2.pk).update(
            next_inspection_date=today + timedelta(days=70)
        )
        coming = month.report(today=today).coming
        assert [(c.kind, c.lift, c.date, c.days_left) for c in coming] == [
            ("maintenance", "L1", date(2026, 11, 20), None),
            ("inspection", "L1", today + timedelta(days=50), 50),
        ]

    def test_lead_precedence(self, month):
        report = month.report()
        assert (report.lead.key, report.lead.count) == ("all_done", 0)

        with company_context(month.company.id):
            month.fault(
                month.l1,
                "EEEEE1",
                at(date(2026, 10, 5), 10),
                priority=FaultPriority.EMERGENCY,
                on_site_at=at(date(2026, 10, 5), 10, 30),
            )
        lead = month.report().lead
        assert (lead.key, lead.count, lead.minutes) == ("emergencies", 1, 30)

        with company_context(month.company.id):
            month.planned(month.l1, OCT, PlannedVisitStatus.PLANNED)
            month.planned(month.l2, OCT, PlannedVisitStatus.PLANNED)
        lead = month.report().lead
        assert (lead.key, lead.count) == ("missed", 2)

        with company_context(month.company.id):
            old = month.inspection(
                month.l3, date(2026, 9, 1), label=Label.RED, correction_deadline=date(2026, 10, 25)
            )
            NonConformity.objects.create(
                company=month.company, inspection=old, description="x", severity=Label.RED
            )
        lead = month.report().lead
        assert (lead.key, lead.count) == ("correction_passed", 1)

    def test_a_report_holds_only_its_customer(self, month):
        with company_context(month.company.id):
            mine = month.visit(month.l1, date(2026, 10, 8))
            theirs = month.visit(month.b1, date(2026, 10, 9))
            month.fault(month.l1, "MINE11", at(date(2026, 10, 5), 10))
            month.fault(month.b1, "THEIRS", at(date(2026, 10, 5), 10))
            month.inspection(month.b1, date(2026, 10, 4), label=Label.RED)
        report = month.report()
        assert {s.name for s in report.lifts} == {"L1", "L2", "L3"}
        dates = {line.date for s in report.lifts for line in s.visits}
        assert dates == {mine.visit_date}
        assert theirs.visit_date not in dates
        assert {f.reference for s in report.lifts for f in s.faults} == {"MINE11"}
        assert [i for s in report.lifts for i in s.inspections] == []

    def test_a_lift_leaving_mid_month_is_covered_for_its_days(self, month):
        with company_context(month.company.id):
            leaving = month.lift(month.zeytin, "LEAVING")
            month.cover(leaving, month.contract_a, date(2026, 1, 1), date(2026, 10, 15))
            month.visit(leaving, date(2026, 10, 10))
            month.visit(leaving, date(2026, 10, 20))
            month.fault(
                leaving, "LEAVE1", at(date(2026, 10, 20), 8), priority=FaultPriority.EMERGENCY
            )
            month.planned(leaving, OCT, PlannedVisitStatus.CANCELLED)
        report = month.report()
        section = next(s for s in report.lifts if s.name == "LEAVING")
        assert [line.date for line in section.visits] == [date(2026, 10, 10)]
        assert section.faults == []
        assert section.missed is None
        assert section.was_missed is False
        assert report.lead.key == "all_done"
        assert report.summary.visits_planned == 0
        assert report.summary.faults_opened == 0
        assert report.summary.faults_open_at_end == 0

    def test_an_inspection_outside_a_lifts_cover_leaves_the_summary_alone(self, month):
        with company_context(month.company.id):
            leaving = month.lift(month.zeytin, "LEAVING")
            month.cover(leaving, month.contract_a, date(2026, 1, 1), date(2026, 10, 15))
            late = month.inspection(
                leaving, date(2026, 10, 20), label=Label.RED, correction_deadline=date(2026, 10, 5)
            )
            NonConformity.objects.create(
                company=month.company, inspection=late, description="x", severity=Label.RED
            )
        report = month.report()
        assert report.summary.open_corrections == 0
        assert [c for c in report.coming if c.kind == "correction"] == []
        assert report.lead.key == "all_done"

    def test_a_customer_without_covered_lifts_gets_an_empty_report(self, month):
        with company_context(month.company.id):
            lonely = Customer.objects.create(
                company=month.company,
                type=CustomerType.CORPORATE,
                legal_name="Lonely",
                tax_number=tax_number(13),
            )
        report = month.report(lonely)
        assert report.lifts == []
        assert report.summary.lifts == 0

    def test_a_correction_is_passed_when_due_by_the_months_last_day(self, month):
        def lead_for(deadline: date) -> str:
            with company_context(month.company.id):
                Inspection.objects.all().delete()
                inspection = month.inspection(
                    month.l1, date(2026, 9, 1), label=Label.RED, correction_deadline=deadline
                )
                NonConformity.objects.create(
                    company=month.company,
                    inspection=inspection,
                    description="x",
                    severity=Label.RED,
                )
            # Built months later: the lead belongs to the month, not to today.
            return month.report(today=date(2027, 3, 1)).lead.key

        assert lead_for(date(2026, 11, 1)) == "all_done"
        assert lead_for(date(2026, 10, 31)) == "correction_passed"

    def test_a_customer_with_portal_access_gets_the_portal_address(self, month):
        from apps.customers.models import CustomerContact
        from apps.portal.models import PortalAccount

        with company_context(month.company.id):
            contact = CustomerContact.objects.create(
                company=month.company, customer=month.a, full_name="C", email="c@example.com"
            )
            PortalAccount.objects.create(
                company=month.company,
                customer=month.a,
                contact=contact,
                email="c@example.com",
                is_active=True,
            )
        assert month.report().portal_url.endswith("/portal")

    def test_query_count_is_flat(self, month):
        def count(customer: Customer) -> int:
            with CaptureQueriesContext(connection) as queries:
                month.report(customer)
            return len(queries)

        with company_context(month.company.id):
            # Customer B has one lift, customer A three; each lift has the lot.
            for index, lift in enumerate([month.b1, month.l1, month.l2, month.l3]):
                visit = month.visit(lift, date(2026, 10, 9))
                MaintenanceVisitItem.objects.create(
                    company=month.company,
                    visit=visit,
                    item=ChecklistItem.BRAKE_SYSTEM,
                    result=ChecklistResult.ATTENTION_NEEDED,
                    note="n",
                )
                month.planned(lift, OCT, PlannedVisitStatus.PLANNED)
                month.fault(lift, f"FLAT1{index}", at(date(2026, 10, 5), 10))
                red = month.inspection(
                    lift, date(2026, 10, 3), correction_deadline=date(2026, 11, 20)
                )
                NonConformity.objects.create(
                    company=month.company, inspection=red, description="x", severity=Label.RED
                )
        assert count(month.a) == count(month.b)


SIGNATURE_ID = "00000000-0000-0000-0000-0000000000a1"
PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def make_data(**changes: Any) -> ReportData:
    visit = VisitLine(
        date=date(2026, 10, 9),
        technician_first_name="Ali",
        status="completed",
        attention=[("brake_system", "Pad worn")],
        parts="Belt",
        signature_state="signed",
        signer_name="Ayşe Kaya",
        signer_role="manager",
        signed_at=at(date(2026, 10, 9), 11),
        amended=False,
        signature_attachment_id=SIGNATURE_ID,
    )
    nobody = VisitLine(
        date=date(2026, 10, 12),
        technician_first_name="Ali",
        status="completed",
        attention=[],
        parts="",
        signature_state="not_present",
        signer_name="",
        signer_role="",
        signed_at=None,
        amended=False,
        signature_attachment_id=None,
    )
    fault = FaultLine(
        reference="F-42",
        reported_at=at(date(2026, 10, 5), 10),
        trapped=False,
        description="Door stuck",
        status="resolved",
        response_minutes=42,
    )
    inspection = InspectionLine(
        date=date(2026, 10, 3),
        label="red",
        body="TSE",
        nonconformities=[("Rope frayed", "open")],
        correction_deadline=date(2026, 11, 20),
        correction_open=True,
    )
    lift = LiftSection(
        name="Lift A",
        building="Zeytin Apt",
        registration="REG-1",
        visits=[visit, nobody],
        missed=date(2026, 9, 8),
        was_missed=True,
        faults=[fault],
        inspections=[inspection],
    )
    summary = ReportSummary(
        lifts=1,
        visits_planned=1,
        visits_completed=0,
        visits_by_status={"completed": 2, "incomplete": 0, "access_denied": 0},
        signatures={"signed": 1, "not_present": 1, "declined": 0},
        faults_opened=1,
        faults_resolved=1,
        faults_open_at_end=0,
        emergencies=0,
        emergency_response_minutes=None,
        open_corrections=1,
    )
    values: dict[str, Any] = {
        "company_name": "Acme Asansör",
        "logo_attachment_id": None,
        "customer_name": "A Sitesi",
        "period": "2026-10",
        "period_label": "Ekim 2026",
        "generated_on": date(2026, 11, 1),
        "summary": summary,
        "lead": Lead("missed", 2),
        "lifts": [lift],
        "coming": [ComingItem("maintenance", "Lift A", date(2026, 11, 9), None)],
        "firm_phone": "0212 000 00 00",
        "firm_email": "info@acme.test",
        "portal_url": None,
    }
    values.update(changes)
    return ReportData(**values)


class TestDocument:
    def test_every_section_is_there(self):
        html = build_html(make_data(), images={SIGNATURE_ID: PIXEL})
        for text in (
            "Ekim 2026",
            "A Sitesi",
            "2 asansörün bakımı bu ay yapılamadı.",
            "Bu ay bakım yapılmadı — son bakım 08.09.2026",
            "Ayşe Kaya",
            "Yönetici",
            "Binada kimse yoktu",
            "F-42",
            "42 dk",
            "Düzeltme son tarihi",
            "Bu rapor ShiftLush kayıtlarından otomatik olarak hazırlanmıştır.",
        ):
            assert text in html, text
        assert PIXEL in html

    def test_empty_sections_are_left_out_but_summary_and_coming_stay(self):
        quiet = LiftSection(name="Lift A", building="B", registration="R")
        html = build_html(make_data(lifts=[quiet], coming=[]), images={})
        assert "Arızalar</span>" not in html
        assert "Muayeneler" not in html
        assert "Özet" in html
        assert "Sırada" in html

    def test_a_never_visited_lift_still_says_it_was_missed(self):
        lift = LiftSection(name="Lift A", building="B", registration="R", was_missed=True)
        html = build_html(make_data(lifts=[lift]), images={})
        assert "Bu ay bakım yapılmadı" in html
        assert "son bakım" not in html

    def test_a_missing_thumbnail_does_not_fail_the_report(self):
        html = build_html(make_data(), images={})
        assert "Ayşe Kaya" in html
        assert "<img" not in html

    def test_html_escapes_record_text(self):
        fault = FaultLine(
            reference="F-1",
            reported_at=at(date(2026, 10, 5), 10),
            trapped=False,
            description="<script>alert(1)</script>",
            status="open",
            response_minutes=None,
        )
        lift = LiftSection(name="Lift A", building="B", registration="R", faults=[fault])
        html = build_html(make_data(lifts=[lift]), images={})
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_render_pdf_makes_a_pdf(self, monkeypatch):
        pytest.importorskip("weasyprint")
        monkeypatch.setattr(
            "apps.reports.documents.fetch_images", lambda data: {SIGNATURE_ID: PIXEL}
        )
        try:
            pdf = render_pdf(make_data())
        except PdfRenderingUnavailable:  # raised only when weasyprint cannot be imported
            pytest.skip("WeasyPrint cannot load its system libraries here")
        assert pdf.startswith(b"%PDF")


# Generating, storing, sending ------------------------------------------------

NEXT_MONTH = date(2026, 11, 1)
PDF = b"%PDF-fake"


@pytest.fixture
def pdf(monkeypatch):
    monkeypatch.setattr("apps.reports.documents.render_pdf", lambda data: PDF)


@pytest.fixture
def bucket(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda backend: fake)
    return fake


def report_mails() -> list[Any]:
    """The reports e-mailed. The office's own notice goes out too, and is not one."""
    return [m for m in django_mail.outbox if m.attachments]


def run_job(today: date, monkeypatch) -> dict[str, int]:
    monkeypatch.setattr("apps.reports.tasks.local_today", lambda: today)
    return monthly_customer_reports.func()


def tick(month: Month, customer: Customer, email: str = "c@example.com", **fields: Any):
    with company_context(month.company.id):
        return CustomerContact.objects.create(
            company=month.company,
            customer=customer,
            full_name="Contact",
            email=email,
            receives_monthly_report=True,
            **fields,
        )


def switch(month: Month, on: bool) -> None:
    Company.objects.filter(pk=month.company.pk).update(monthly_reports_enabled=on)


def reports(month: Month) -> list[CustomerReport]:
    found = CustomerReport.unscoped.filter(company=month.company, is_deleted=False)
    return list(found.order_by("customer__legal_name"))


@pytest.mark.usefixtures("pdf", "bucket")
class TestGeneration:
    def test_the_first_makes_last_months_reports(self, month, monkeypatch):
        result = run_job(NEXT_MONTH, monkeypatch)
        made = reports(month)
        assert [(r.customer_id, r.period) for r in made] == [(month.a.pk, OCT), (month.b.pk, OCT)]
        assert result["created"] == 2
        for report in made:
            file = Attachment.unscoped.get(pk=str(report.file_id))
            assert file.category == AttachmentCategory.MONTHLY_REPORT
            assert file.object_id == report.customer_id
            assert report.summary["lifts"] >= 1
            assert report.summary["lead"]["key"]
            assert report.generated_at is not None

    def test_a_customer_without_a_covered_lift_gets_none(self, month, monkeypatch):
        with company_context(month.company.id):
            Customer.objects.create(
                company=month.company,
                type=CustomerType.CORPORATE,
                legal_name="C Empty",
                tax_number=tax_number(13),
            )
        run_job(NEXT_MONTH, monkeypatch)
        assert len(reports(month)) == 2

    def test_switch_off_sends_nothing(self, month, monkeypatch, django_capture_on_commit_callbacks):
        tick(month, month.a)
        with django_capture_on_commit_callbacks(execute=True):
            run_job(NEXT_MONTH, monkeypatch)
        assert {r.delivery for r in reports(month)} == {ReportDelivery.NOT_ENABLED}
        assert report_mails() == []

    def test_switch_on_sends_one_mail_per_ticked_contact_with_the_pdf(
        self, month, monkeypatch, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a, "one@example.com")
        tick(month, month.a, "two@example.com")
        with django_capture_on_commit_callbacks(execute=True):
            run_job(NEXT_MONTH, monkeypatch)
        assert len(report_mails()) == 2
        for message in report_mails():
            assert message.subject == "Month — Ekim 2026 bakım raporu (A Sitesi)"
            assert len(message.attachments) == 1
            _, content, mime_type = message.attachments[0]
            assert (content, mime_type) == (PDF, "application/pdf")
        assert sorted(m.to[0] for m in report_mails()) == ["one@example.com", "two@example.com"]
        report = CustomerReport.unscoped.get(customer=month.a)
        assert report.delivery == ReportDelivery.SENT
        assert report.recipients == ["one@example.com", "two@example.com"]
        assert report.sent_at is not None

    def test_a_blank_address_is_skipped_even_when_ticked(self, month, monkeypatch):
        switch(month, True)
        tick(month, month.a, "")
        run_job(NEXT_MONTH, monkeypatch)
        assert CustomerReport.unscoped.get(customer=month.a).delivery == ReportDelivery.NO_RECIPIENT

    def test_nobody_ticked_is_no_recipient(self, month, monkeypatch):
        switch(month, True)
        result = run_job(NEXT_MONTH, monkeypatch)
        assert {r.delivery for r in reports(month)} == {ReportDelivery.NO_RECIPIENT}
        assert result["no_recipient"] == 2
        assert report_mails() == []

    def test_a_second_run_sends_nothing(
        self, month, monkeypatch, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a)
        with django_capture_on_commit_callbacks(execute=True):
            run_job(NEXT_MONTH, monkeypatch)
            second = run_job(NEXT_MONTH, monkeypatch)
        assert second["created"] == 0
        assert len(reports(month)) == 2
        assert len(report_mails()) == 1

    def test_a_late_run_catches_up_but_stops_mailing_after_seven_days(
        self, month, monkeypatch, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a)
        with django_capture_on_commit_callbacks(execute=True):
            run_job(date(2026, 11, 5), monkeypatch)
        assert len(reports(month)) == 2
        assert len(report_mails()) == 1

        CustomerReport.unscoped.all().delete()
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            run_job(date(2026, 11, 9), monkeypatch)
        assert {r.delivery for r in reports(month)} == {ReportDelivery.NOT_ENABLED}
        assert report_mails() == []

    def test_one_failure_does_not_stop_the_rest(self, month, monkeypatch, caplog):
        def render(data):
            if data.customer_name == "A Sitesi":
                raise RuntimeError("boom")
            return PDF

        monkeypatch.setattr("apps.reports.documents.render_pdf", render)
        result = run_job(NEXT_MONTH, monkeypatch)
        assert [r.customer_id for r in reports(month)] == [month.b.pk]
        assert result["failed"] == 1
        assert result["created"] == 1
        assert "failed" in caplog.text

    def test_a_failed_send_marks_the_report_failed(
        self, month, monkeypatch, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a)

        def broken(self, fail_silently=False):
            raise OSError("smtp down")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
        with django_capture_on_commit_callbacks(execute=True):
            run_job(NEXT_MONTH, monkeypatch)
        report = CustomerReport.unscoped.get(customer=month.a)
        assert report.delivery == ReportDelivery.FAILED
        assert report.error

    def test_the_office_is_told_once(self, month, monkeypatch):
        with system_context():
            admin = User.objects.create_user(
                email="admin-month@example.com",
                password=PASSWORD,
                company=month.company,
                first_name="A",
                last_name="Min",
                role=Role.ADMIN,
            )
        switch(month, True)
        tick(month, month.a)
        run_job(NEXT_MONTH, monkeypatch)
        run_job(NEXT_MONTH, monkeypatch)
        told = Notification.unscoped.filter(kind=NotificationKind.MONTHLY_REPORTS_READY)
        assert {n.recipient_id for n in told} == {month.owner.pk, admin.pk}
        data = told[0].data
        assert (data["period"], data["count"], data["sent"], data["no_recipient"]) == (
            OCT,
            2,
            1,
            1,
        )

    def test_regenerate_replaces_the_file_and_keeps_the_old(self, month):
        with system_context():
            report = generate_one(month.a, OCT, today=NEXT_MONTH)
            assert report is not None
            old = report.file
            assert old is not None
            again = regenerate(report, today=NEXT_MONTH)
        assert again.file_id != old.pk
        assert Attachment.unscoped.get(pk=old.pk).is_deleted
        assert not Attachment.unscoped.get(pk=str(again.file_id)).is_deleted

    def test_send_report_refuses_when_nobody_is_ticked(self, month):
        with system_context():
            report = generate_one(month.a, OCT, today=NEXT_MONTH)
            assert report is not None
            with pytest.raises(Conflict) as caught:
                send_report(report, by=month.owner)
        assert caught.value.get_codes() == "REPORT_NO_RECIPIENT"

    def test_the_schedule_runs_the_job_at_eight(self):
        names = {s.name: s for s in get_schedules()}
        assert names["monthly_customer_reports"].at == time(8, 0)


@pytest.mark.usefixtures("pdf")
class TestGenerationFixes:
    def test_a_lost_race_is_skipped_quietly(
        self, month, monkeypatch, bucket, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a)
        with system_context():
            CustomerReport.objects.create(company=month.company, customer=month.a, period=OCT)
        real = services._report_exists
        missed: list[int] = []

        def blind_once(customer, period):
            if customer.pk == month.a.pk and not missed:
                missed.append(1)
                return False
            return real(customer, period)

        monkeypatch.setattr(services, "_report_exists", blind_once)
        with django_capture_on_commit_callbacks(execute=True):
            result = run_job(NEXT_MONTH, monkeypatch)
        assert missed
        assert (result["created"], result["failed"]) == (1, 0)
        assert report_mails() == []
        assert not any(str(month.a.pk) in key for _, key in bucket.objects)

    def test_a_failure_after_storing_leaves_no_object(self, month, monkeypatch, bucket):
        def boom(*args, **kwargs):
            raise RuntimeError("after the store")

        monkeypatch.setattr(services, "_deliver", boom)
        result = run_job(NEXT_MONTH, monkeypatch)
        assert result["failed"] == 2
        assert bucket.objects == {}

    def test_a_failing_failure_record_does_not_stop_the_other_mails(
        self, month, monkeypatch, bucket, django_capture_on_commit_callbacks
    ):
        switch(month, True)
        tick(month, month.a, "one@example.com")
        tick(month, month.a, "two@example.com")
        attempts: list[str] = []

        def broken(self, fail_silently=False):
            attempts.append(self.to[0])
            raise OSError("smtp down")

        def cannot_record(*args, **kwargs):
            raise RuntimeError("database gone")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
        monkeypatch.setattr(services, "_mark_failed", cannot_record)
        with django_capture_on_commit_callbacks(execute=True):
            result = run_job(NEXT_MONTH, monkeypatch)
        assert [a for a in attempts if a != month.owner.email] == [
            "one@example.com",
            "two@example.com",
        ]
        assert result["failed"] == 0

    def test_a_partial_failure_says_who_did_not_get_it(
        self, month, monkeypatch, bucket, django_capture_on_commit_callbacks
    ):
        from django.core.mail import EmailMessage

        switch(month, True)
        tick(month, month.a, "good@example.com")
        tick(month, month.a, "bad@example.com")
        tick(month, month.a, "worse@example.com")
        real = EmailMessage.send

        def flaky(self, fail_silently=False):
            if self.to[0] != "good@example.com":
                raise OSError("smtp down")
            return real(self, fail_silently=fail_silently)

        monkeypatch.setattr(EmailMessage, "send", flaky)
        with django_capture_on_commit_callbacks(execute=True):
            run_job(NEXT_MONTH, monkeypatch)
        report = CustomerReport.unscoped.get(customer=month.a)
        assert report.delivery == ReportDelivery.FAILED
        assert "Partially" in report.error
        assert "bad@example.com" in report.error
        assert "worse@example.com" in report.error
        assert "good@example.com" not in report.error


# The API: the office and the portal ---------------------------------------------


def staff(month: Month, role: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=f"{role}-api@example.com",
            password=PASSWORD,
            company=month.company,
            first_name="S",
            last_name=role,
            role=role,
        )


def made(month: Month, customer: Customer, period: str = OCT) -> CustomerReport:
    with system_context():
        report = generate_one(customer, period, today=NEXT_MONTH)
    assert report is not None
    return report


KEY = {"Idempotency-Key": "k-1"}


@pytest.mark.usefixtures("pdf", "bucket")
class TestApi:
    def test_office_lists_and_filters(self, month):
        mine = made(month, month.a)
        made(month, month.b)
        made(month, month.a, SEP)
        client = api_for(month.owner)
        url = reverse("customer-report-list")
        assert len(client.get(url).data["results"]) == 3
        by_customer = client.get(url, {"customer": str(month.a.pk)}).data["results"]
        assert {str(row["customer"]) for row in by_customer} == {str(month.a.pk)}
        assert [
            r["id"]
            for r in client.get(url, {"period": OCT, "customer": str(month.a.pk)}).data["results"]
        ] == [str(mine.pk)]
        assert client.get(url, {"delivery": "sent"}).data["results"] == []
        assert len(client.get(url, {"delivery": "pending"}).data["results"]) == 3
        detail = client.get(reverse("customer-report-detail", args=[mine.pk]))
        assert detail.status_code == 200
        assert str(detail.data["file"]) == str(mine.file_id)
        assert detail.data["customer_name"] == "A Sitesi"
        assert api_for(staff(month, Role.ACCOUNTANT)).get(url).status_code == 200
        assert api_for(month.tech).get(url).status_code == 403

    def test_send_needs_a_key_and_a_recipient(self, month, django_capture_on_commit_callbacks):
        report = made(month, month.a)
        client = api_for(month.owner)
        url = reverse("customer-report-send", args=[report.pk])
        missing = client.post(url, {}, format="json")
        assert missing.status_code == 400
        assert missing.data["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        nobody = client.post(url, {}, format="json", headers=KEY)
        assert nobody.status_code == 409
        assert nobody.data["error"]["code"] == "REPORT_NO_RECIPIENT"
        tick(month, month.a)
        with django_capture_on_commit_callbacks(execute=True):
            sent = client.post(url, {}, format="json", headers=KEY)
        assert sent.status_code == 200
        assert sent.data["delivery"] == "sent"
        assert len(report_mails()) == 1

    def test_resend_goes_to_current_recipients(self, month, django_capture_on_commit_callbacks):
        report = made(month, month.a)
        first = tick(month, month.a, "a@example.com")
        client = api_for(month.owner)
        url = reverse("customer-report-send", args=[report.pk])
        with django_capture_on_commit_callbacks(execute=True):
            assert (
                client.post(url, {}, format="json", headers={"Idempotency-Key": "one"}).status_code
                == 200
            )
        with company_context(month.company.id):
            CustomerContact.objects.filter(pk=first.pk).update(receives_monthly_report=False)
        tick(month, month.a, "b@example.com")
        with django_capture_on_commit_callbacks(execute=True):
            again = client.post(url, {}, format="json", headers={"Idempotency-Key": "two"})
        assert again.status_code == 200
        assert again.data["recipients"] == ["b@example.com"]
        assert [m.to for m in report_mails()] == [["a@example.com"], ["b@example.com"]]

    def test_a_failed_send_reads_failed_afterwards(
        self, month, monkeypatch, django_capture_on_commit_callbacks
    ):
        report = made(month, month.a)
        tick(month, month.a)

        def broken(self, fail_silently=False):
            raise OSError("smtp down")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
        with django_capture_on_commit_callbacks(execute=True):
            api_for(month.owner).post(
                reverse("customer-report-send", args=[report.pk]), {}, format="json", headers=KEY
            )
        stored = CustomerReport.unscoped.get(pk=report.pk)
        assert stored.delivery == ReportDelivery.FAILED
        assert stored.error

    def test_send_is_not_for_the_accountant(self, month):
        report = made(month, month.a)
        client = api_for(staff(month, Role.ACCOUNTANT))
        url = reverse("customer-report-send", args=[report.pk])
        assert client.post(url, {}, format="json", headers=KEY).status_code == 403

    def test_preview_renders_without_storing(self, month):
        before = (CustomerReport.unscoped.count(), Attachment.unscoped.count())
        client = api_for(month.owner)
        for period in (OCT, "2026-11"):
            response = client.post(
                reverse("customer-report-preview"),
                {"customer": str(month.a.pk), "period": period},
                format="json",
            )
            assert response.status_code == 200, response.data
            assert response["Content-Type"] == "application/pdf"
            assert response.content == PDF
        assert (CustomerReport.unscoped.count(), Attachment.unscoped.count()) == before

    def test_preview_without_a_renderer_is_unavailable(self, month, monkeypatch):
        def missing(data):
            raise PdfRenderingUnavailable("no")

        monkeypatch.setattr("apps.reports.documents.render_pdf", missing)
        response = api_for(month.owner).post(
            reverse("customer-report-preview"),
            {"customer": str(month.a.pk), "period": OCT},
            format="json",
        )
        assert response.status_code == 503

    def test_preview_refuses_a_bad_period(self, month):
        response = api_for(month.owner).post(
            reverse("customer-report-preview"),
            {"customer": str(month.a.pk), "period": "2026-13"},
            format="json",
        )
        assert response.status_code == 400

    def test_regenerate_is_owner_or_admin(self, month):
        report = made(month, month.a)
        url = reverse("customer-report-regenerate", args=[report.pk])
        assert api_for(staff(month, Role.OPERATIONS)).post(url).status_code == 403
        assert api_for(month.tech).post(url).status_code == 403
        response = api_for(staff(month, Role.ADMIN)).post(url)
        assert response.status_code == 200
        assert str(response.data["file"]) != str(report.file_id)

    def test_portal_lists_its_own_customers_reports_and_downloads_them(self, month):
        mine = made(month, month.a)
        theirs = made(month, month.b)
        with company_context(month.company.id):
            contact = CustomerContact.objects.create(
                company=month.company, customer=month.a, full_name="C", email="c@example.com"
            )
            _, token = portal_services.invite(customer=month.a, contact=contact)
        account = portal_services.accept(token=token, password=PASSWORD)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {portal_services.issue_tokens(account).access}"
        )
        rows = client.get(reverse("portal-report-list")).data["results"]
        assert [row["id"] for row in rows] == [str(mine.pk)]
        assert "error" not in rows[0] and "recipients" not in rows[0]
        own = client.get(reverse("portal-file-download-url", args=[mine.file_id]))
        assert own.status_code == 200
        other = client.get(reverse("portal-file-download-url", args=[theirs.file_id]))
        assert other.status_code == 404
        assert client.get(reverse("portal-report-detail", args=[theirs.pk])).status_code == 404


# Final review: the report file is the server's to make and keep ----------------


def lose_file(report: CustomerReport) -> CustomerReport:
    """Soft-delete the report's file behind its back, as the old DELETE could."""
    Attachment.unscoped.filter(pk=str(report.file_id)).update(
        is_deleted=True, deleted_at=at(NEXT_MONTH, 9)
    )
    return CustomerReport.unscoped.get(pk=report.pk)


@pytest.mark.usefixtures("pdf", "bucket")
class TestReportFiles:
    def test_a_report_file_cannot_be_deleted_through_attachments(self, month):
        report = made(month, month.a)
        response = api_for(month.owner).delete(reverse("attachment-detail", args=[report.file_id]))
        assert (response.status_code, response.data["error"]["code"]) == (
            409,
            "REPORT_FILE_CANNOT_BE_DELETED",
        )
        assert not Attachment.unscoped.get(pk=str(report.file_id)).is_deleted

    def test_regenerate_renders_a_new_file_when_the_old_one_is_gone(self, month):
        report = lose_file(made(month, month.a))
        response = api_for(month.owner).post(
            reverse("customer-report-regenerate", args=[report.pk])
        )
        assert response.status_code == 200, response.data
        assert str(response.data["file"]) != str(report.file_id)
        assert not Attachment.unscoped.get(pk=response.data["file"]).is_deleted
        with system_context():
            CustomerReport.unscoped.filter(pk=report.pk).update(file=None)
            again = regenerate(CustomerReport.unscoped.get(pk=report.pk), today=NEXT_MONTH)
        assert again.file_id is not None

    def test_send_renders_the_file_again_when_it_is_gone(
        self, month, django_capture_on_commit_callbacks
    ):
        report = lose_file(made(month, month.a))
        tick(month, month.a)
        with django_capture_on_commit_callbacks(execute=True):
            response = api_for(month.owner).post(
                reverse("customer-report-send", args=[report.pk]), {}, format="json", headers=KEY
            )
        assert response.status_code == 200, response.data
        assert response.data["delivery"] == "sent"
        assert str(response.data["file"]) != str(report.file_id)
        assert [m.attachments[0][1] for m in report_mails()] == [PDF]

    def test_a_technician_cannot_reach_report_files(self, month):
        report = made(month, month.a)
        with system_context():
            month.tech.customer_assignments.create(company=month.company, customer=month.a)
        client = api_for(month.tech)
        listed = client.get(reverse("attachment-list")).data["results"]
        assert str(report.file_id) not in {row["id"] for row in listed}
        download = client.get(reverse("attachment-download-url", args=[report.file_id]))
        assert download.status_code == 404

    def test_the_accountant_downloads_a_report_and_the_technician_does_not(self, month):
        report = made(month, month.a)
        url = reverse("customer-report-download-url", args=[report.pk])
        response = api_for(staff(month, Role.ACCOUNTANT)).get(url)
        assert response.status_code == 200, response.data
        assert set(response.data) == {"download_url", "expires_in"}
        assert (
            Attachment.unscoped.get(pk=str(report.file_id)).storage_key
            in response.data["download_url"]
        )
        assert api_for(month.tech).get(url).status_code == 403

    def test_the_job_skips_a_deactivated_firm(self, month, monkeypatch):
        Company.objects.filter(pk=month.company.pk).update(is_active=False)
        result = run_job(NEXT_MONTH, monkeypatch)
        assert reports(month) == []
        assert result["created"] == 0

    def test_nobody_uploads_a_monthly_report(self, month, bucket):
        client = api_for(month.owner)
        ticket = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.CUSTOMER,
                "object_id": str(month.a.pk),
                "category": AttachmentCategory.MONTHLY_REPORT,
                "mime_type": "application/pdf",
                "size_bytes": 1000,
            },
            format="json",
        )
        refused = [{"field": "category", "code": "CATEGORY_NOT_UPLOADABLE"}]
        assert ticket.status_code == 400
        assert ticket.data["error"]["details"] == refused
        key = f"{month.company.id}/customer/{month.a.pk}/monthly_report/forged.pdf"
        bucket.put("shiftlush-dev", key, 1000, "application/pdf")
        confirmed = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "r.pdf"},
            format="json",
        )
        assert confirmed.status_code == 400
        assert confirmed.data["error"]["details"] == refused
        assert not Attachment.unscoped.filter(storage_key=key).exists()
        assert ("shiftlush-dev", key) not in bucket.objects
