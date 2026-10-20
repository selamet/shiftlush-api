"""Attachments against a maintenance visit - see apps.attachments.services._target_exists."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.attachments.services import TARGET_MODELS, _target_exists
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import register_company
from core import storage
from core.context import company_context
from tests.test_attachments import FakeS3, uploaded

PASSWORD = "correct-horse-battery"


@pytest.fixture
def bucket(monkeypatch) -> FakeS3:
    """Same fake as tests/test_attachments.py - see FakeS3 there for why the
    bucket is faked at the boto3 client rather than at core.storage.
    """
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda backend: fake)
    return fake


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


class TestTechnicianAttachmentCarveOut:
    def test_technician_can_request_an_upload_url_for_a_visit_photo(self, a_visit):
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech2@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=visit.elevator.building.customer
            )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.MAINTENANCE_VISIT,
                "object_id": str(visit.id),
                "category": AttachmentCategory.PHOTO,
                "mime_type": "image/jpeg",
                "size_bytes": 1000,
            },
        )
        assert response.status_code == 200, response.data

    def test_technician_cannot_request_an_upload_url_for_a_signed_contract(self, a_visit):
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech3@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=visit.elevator.building.customer
            )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.CONTRACT,
                "object_id": str(visit.contract_id),
                "category": AttachmentCategory.SIGNED_CONTRACT,
                "mime_type": "application/pdf",
                "size_bytes": 1000,
            },
        )
        assert response.status_code == 403

    def test_technician_cannot_request_an_upload_url_for_a_visit_outside_their_assignment(
        self, a_visit
    ):
        """Closes the gap the design doc's "Attachment permission carve-out"
        paragraph promised but the first pass never implemented: object_type
        and category matching a photo on a maintenance visit is not enough by
        itself, the visit also has to belong to a customer this technician is
        assigned to.
        """
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            other_customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Somebody else's"
            )
            technician = User.objects.create_user(
                company=company,
                email="tech7@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            # Assigned to a real customer of this company - just not the one
            # this visit belongs to.
            technician.customer_assignments.create(company=company, customer=other_customer)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.MAINTENANCE_VISIT,
                "object_id": str(visit.id),
                "category": AttachmentCategory.PHOTO,
                "mime_type": "image/jpeg",
                "size_bytes": 1000,
            },
        )
        assert response.status_code == 403

    def test_technician_can_confirm_a_photo_upload_for_their_own_visit(self, a_visit, bucket):
        """The full round trip over real HTTP, not just the upload-url half."""
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech8@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=visit.elevator.building.customer
            )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        ticket = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.MAINTENANCE_VISIT,
                "object_id": str(visit.id),
                "category": AttachmentCategory.PHOTO,
                "mime_type": "image/jpeg",
                "size_bytes": 1000,
            },
        )
        key = ticket.data["storage_key"]
        uploaded(bucket, key)

        response = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "visit.jpg"},
        )
        assert response.status_code == 201, response.data

    def test_technician_cannot_confirm_a_photo_for_an_unassigned_customers_visit(
        self, a_visit, bucket
    ):
        """Defense in depth: even a technician who somehow obtained a valid
        storage key for a visit outside their assignment - one a privileged
        role requested, say - cannot confirm it into a record.
        """
        from apps.attachments.services import prepare_upload
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            other_customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Somebody else's"
            )
            technician = User.objects.create_user(
                company=company,
                email="tech9@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(company=company, customer=other_customer)

            # As if a privileged role had requested this - the technician
            # never called upload-url themselves.
            ticket = prepare_upload(
                company_id=company.id,
                object_type=ObjectType.MAINTENANCE_VISIT,
                object_id=visit.id,
                category=AttachmentCategory.PHOTO,
                mime_type="image/jpeg",
                size_bytes=1000,
            )
        uploaded(bucket, ticket.storage_key)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-list"),
            {"storage_key": ticket.storage_key, "original_filename": "visit.jpg"},
        )
        assert response.status_code == 403


class TestPeekObjectTypeAndCategory:
    def test_reads_back_what_prepare_upload_wrote(self, a_visit):
        from apps.attachments.services import peek_object_type_and_category, prepare_upload

        company, visit = a_visit
        ticket = prepare_upload(
            company_id=company.id,
            object_type=ObjectType.MAINTENANCE_VISIT,
            object_id=visit.id,
            category=AttachmentCategory.PHOTO,
            mime_type="image/jpeg",
            size_bytes=1000,
        )
        object_type, object_id, category = peek_object_type_and_category(
            company.id, ticket.storage_key
        )
        assert object_type == ObjectType.MAINTENANCE_VISIT
        assert object_id == visit.id
        assert category == AttachmentCategory.PHOTO

    def test_returns_none_none_none_for_garbage(self, a_visit):
        from apps.attachments.services import peek_object_type_and_category

        company, _ = a_visit
        assert peek_object_type_and_category(company.id, "not-a-real-key") == (None, None, None)
