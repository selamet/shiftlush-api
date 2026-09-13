"""Attachments against a maintenance visit - see apps.attachments.services._target_exists."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.attachments.models import ObjectType
from apps.attachments.services import TARGET_MODELS, _target_exists
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.properties.models import Building, BuildingType
from apps.users.services import register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"


@pytest.fixture
def a_visit(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner2@example.com",
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
        elevator = Elevator.objects.create(company=company, building=building, qr_token="c" * 24)
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number="C-2",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=date.today() - timedelta(days=10),
            end_date=date.today() + timedelta(days=355),
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=date.today()
        )
        visit = MaintenanceVisit.objects.create(
            company=company,
            elevator=elevator,
            contract=contract,
            technician=owner,
            visit_date=date.today(),
            status=VisitStatus.COMPLETED,
        )
    return company, visit


class TestMaintenanceVisitIsAnAttachmentTarget:
    def test_object_type_is_registered(self):
        assert "maintenance_visit" in TARGET_MODELS
        assert TARGET_MODELS[ObjectType.MAINTENANCE_VISIT] == "maintenance.MaintenanceVisit"

    def test_target_exists_finds_a_real_visit(self, a_visit):
        company, visit = a_visit
        assert _target_exists(company.id, ObjectType.MAINTENANCE_VISIT, visit.id)

    def test_target_exists_rejects_another_companys_visit(self, a_visit):
        import uuid

        _, visit = a_visit
        assert not _target_exists(uuid.uuid4(), ObjectType.MAINTENANCE_VISIT, visit.id)
