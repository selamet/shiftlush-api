"""Maintenance visit records - specification in
docs/superpowers/specs/2026-09-12-maintenance-visit-design.md.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

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
from apps.users.services import register_company
from core.context import company_context

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
