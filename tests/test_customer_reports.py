"""The monthly customer report: the record, and who it is meant for."""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.address.models import District, Neighborhood, Province
from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
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
from apps.properties.models import Building, BuildingType
from apps.reports.content import build_report, month_bounds
from apps.reports.models import CustomerReport, ReportDelivery
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context
from core.scheduling import LOCAL_TZ
from tests.identifiers import tax_number

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
            month.fault(leaving, "LEAVE1", at(date(2026, 10, 20), 8))
            month.planned(leaving, OCT, PlannedVisitStatus.CANCELLED)
        report = month.report()
        section = next(s for s in report.lifts if s.name == "LEAVING")
        assert [line.date for line in section.visits] == [date(2026, 10, 10)]
        assert section.faults == []
        assert section.missed is None
        assert section.was_missed is False
        assert report.lead.key == "all_done"
        assert report.summary.visits_planned == 0

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
