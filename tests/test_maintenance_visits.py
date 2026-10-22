"""Maintenance visit records - specification in
docs/superpowers/specs/2026-09-12-maintenance-visit-design.md.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from django.urls import reverse
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError

PASSWORD = "correct-horse-battery"


@pytest.fixture
def visit_fixture(db):
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
            company=company, type=CustomerType.CORPORATE, legal_name="Cust"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="B1",
            type=BuildingType.RESIDENTIAL,
            address_note="x",
        )
        elevator = Elevator.objects.create(company=company, building=building, qr_token="b" * 24)
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number="C-1",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() + timedelta(days=335),
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=date.today()
        )
    return company, owner, elevator, contract


class TestMaintenanceVisitModel:
    def test_create_visit_with_checklist_items(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            item = MaintenanceVisitItem.objects.create(
                company=company,
                visit=visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.OK,
            )

            assert visit.items.count() == 1
            assert item.visit_id == visit.id

    def test_same_item_twice_on_one_visit_is_rejected(self, visit_fixture):
        from django.db import IntegrityError

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            MaintenanceVisitItem.objects.create(
                company=company,
                visit=visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.OK,
            )
            with pytest.raises(IntegrityError):
                MaintenanceVisitItem.objects.create(
                    company=company,
                    visit=visit,
                    item=ChecklistItem.BRAKE_SYSTEM,
                    result=ChecklistResult.ATTENTION_NEEDED,
                )


class TestElevatorMaintenanceFields:
    def test_new_fields_default_to_null(self, db):
        from datetime import date

        from apps.companies.models import Company
        from apps.customers.models import Customer, CustomerType
        from apps.properties.models import Building, BuildingType
        from core.context import company_context, system_context

        with system_context():
            company = Company.objects.create(legal_name="Firm", display_name="Firm")
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Cust"
            )
            building = Building.objects.create(
                company=company,
                customer=customer,
                name="B1",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
            )
            elevator = Elevator.objects.create(
                company=company, building=building, qr_token="a" * 24
            )

        assert elevator.last_maintenance_date is None
        assert elevator.next_maintenance_date is None
        assert isinstance(elevator.maintenance_interval_days, int)
        assert date  # type: ignore  # imported for readability of the test body above


class TestEnsureActiveContract:
    def test_rejects_a_contract_that_does_not_cover_the_elevator(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            other_customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Other"
            )
            other_contract = Contract.objects.create(
                company=company,
                customer=other_customer,
                contract_number="C-3",
                status=ContractStatus.ACTIVE,
                scope=Scope.MAINTENANCE_ONLY,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=365),
                pricing_type=PricingType.FLAT,
                monthly_fee="500.00",
                vat_rate="20.00",
            )
            with pytest.raises(BusinessRuleError) as exc:
                services.ensure_active_contract(elevator=elevator, contract=other_contract)
        detail = exc.value.detail
        assert not isinstance(detail, list | dict)
        assert detail.code == ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR.value

    def test_rejects_a_non_active_contract(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            contract.status = ContractStatus.DRAFT
            contract.save(update_fields=["status"])
            with pytest.raises(BusinessRuleError):
                services.ensure_active_contract(elevator=elevator, contract=contract)

    def test_accepts_an_active_contract_covering_the_elevator(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            services.ensure_active_contract(elevator=elevator, contract=contract)  # no raise


class TestRecalculateNextMaintenance:
    def test_no_completed_visit_leaves_dates_null(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            services.recalculate_next_maintenance(elevator)
            # Inside company_context: Elevator is a CompanyOwnedModel, so
            # refresh_from_db() is tenant-scoped and returns nothing once the
            # context exits.
            elevator.refresh_from_db()
        assert elevator.last_maintenance_date is None
        assert elevator.next_maintenance_date is None

    def test_completed_visit_sets_both_dates(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        visit_date = date.today() - timedelta(days=5)
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=visit_date,
                status=VisitStatus.COMPLETED,
            )
            services.recalculate_next_maintenance(elevator)
            elevator.refresh_from_db()
        assert elevator.last_maintenance_date == visit_date
        assert elevator.next_maintenance_date == visit_date + timedelta(
            days=elevator.maintenance_interval_days
        )

    def test_incomplete_visit_does_not_move_the_date(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.ACCESS_DENIED,
            )
            services.recalculate_next_maintenance(elevator)
            elevator.refresh_from_db()
        assert elevator.last_maintenance_date is None

    def test_uses_the_latest_completed_visit_by_date_not_by_creation_order(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        older = date.today() - timedelta(days=40)
        newer = date.today() - timedelta(days=2)
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=newer,
                status=VisitStatus.COMPLETED,
            )
            # Entered second, but an earlier visit date - a backdated entry.
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=older,
                status=VisitStatus.COMPLETED,
            )
            services.recalculate_next_maintenance(elevator)
            elevator.refresh_from_db()
        assert elevator.last_maintenance_date == newer


class TestSetChecklist:
    def test_replaces_the_full_set(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            services.set_checklist(
                visit=visit,
                items=[{"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK}],
            )
            services.set_checklist(
                visit=visit,
                items=[
                    {
                        "item": ChecklistItem.BRAKE_SYSTEM,
                        "result": ChecklistResult.ATTENTION_NEEDED,
                    },
                    {"item": ChecklistItem.HOISTING_ROPES, "result": ChecklistResult.OK},
                ],
            )
            # Inside company_context: MaintenanceVisitItem is a
            # CompanyOwnedModel, so the reverse manager `visit.items` is
            # tenant-scoped and returns nothing once the context exits.
            assert visit.items.count() == 2
            brake = visit.items.get(item=ChecklistItem.BRAKE_SYSTEM)
            assert brake.result == ChecklistResult.ATTENTION_NEEDED


class TestMaintenanceVisitSerializers:
    def test_read_serializer_nests_items(self, visit_fixture):
        from apps.maintenance.api.v1.serializers import MaintenanceVisitReadSerializer

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            MaintenanceVisitItem.objects.create(
                company=company,
                visit=visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.OK,
            )
            data = MaintenanceVisitReadSerializer(visit).data

        assert data["status"] == VisitStatus.COMPLETED
        assert len(data["items"]) == 1
        assert data["items"][0]["item"] == ChecklistItem.BRAKE_SYSTEM

    def test_write_serializer_rejects_unknown_fields(self, visit_fixture):
        from apps.maintenance.api.v1.serializers import MaintenanceVisitWriteSerializer

        company, owner, elevator, contract = visit_fixture
        # The technician field is scoped to the signed-in user's firm, so the
        # serializer needs to know who is asking.
        request = Request(APIRequestFactory().post("/"))
        request.user = owner
        with company_context(company.id):
            serializer = MaintenanceVisitWriteSerializer(
                context={"request": request},
                data={
                    "elevator": str(elevator.id),
                    "contract": str(contract.id),
                    "technician": str(owner.id),
                    "visit_date": str(date.today()),
                    "status": VisitStatus.COMPLETED,
                    "signature_status": "not_present",
                    "not_a_real_field": "x",
                },
            )
            assert not serializer.is_valid()
            assert "not_a_real_field" in serializer.errors


def api_for(user: User) -> APIClient:
    # A fresh key per client: visit creates require one (M1), and each test
    # builds a new client per request.
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    return client


class TestMaintenanceVisitAPI:
    def test_owner_can_create_a_visit_and_set_its_checklist(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
                "signature_status": "not_present",
            },
        )
        assert response.status_code == 201, response.data
        visit_id = response.data["id"]

        checklist_response = api_for(owner).post(
            reverse("maintenance-visit-set-checklist", args=[visit_id]),
            {"items": [{"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK}]},
        )
        assert checklist_response.status_code == 200, checklist_response.data
        assert len(checklist_response.data["items"]) == 1

        with company_context(company.id):
            elevator.refresh_from_db()
            assert elevator.last_maintenance_date == date.today()

    def test_create_without_an_active_contract_is_a_422(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            contract.status = ContractStatus.DRAFT
            contract.save(update_fields=["status"])

        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
                "signature_status": "not_present",
            },
        )
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR.value

    def test_incomplete_status_does_not_move_next_maintenance_date(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.ACCESS_DENIED,
            },
        )
        assert response.status_code == 201, response.data
        with company_context(company.id):
            elevator.refresh_from_db()
            assert elevator.next_maintenance_date is None

    def test_technician_cannot_edit_a_visit(self, visit_fixture):
        from apps.users.models import Role

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            technician = User.objects.create_user(
                company=company,
                email="tech@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=Customer.objects.get(legal_name="Cust")
            )

        response = api_for(technician).patch(
            reverse("maintenance-visit-detail", args=[visit.id]), {"work_notes": "changed"}
        )
        assert response.status_code == 403

    def test_technician_can_create_a_visit_and_set_its_own_checklist(self, visit_fixture):
        from apps.users.models import Role

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech-assigned@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=Customer.objects.get(legal_name="Cust")
            )

        response = api_for(technician).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(technician.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
                "signature_status": "not_present",
            },
        )
        assert response.status_code == 201, response.data
        visit_id = response.data["id"]

        checklist_response = api_for(technician).post(
            reverse("maintenance-visit-set-checklist", args=[visit_id]),
            {"items": [{"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK}]},
        )
        assert checklist_response.status_code == 200, checklist_response.data
        assert len(checklist_response.data["items"]) == 1
