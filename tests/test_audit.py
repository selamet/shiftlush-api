"""The audit trail.

Two things have to hold at once: every write is recorded, and no write records
a secret. A trail that captures password hashes and national IDs turns the
safety net into the largest single leak in the system.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import AuditAction, AuditLog
from apps.customers.models import Customer, CustomerType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.audit import MASK
from core.context import RequestActor, actor_context, company_context, system_context

PASSWORD = "correct-horse-battery"
TCKN = "10000000146"


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
    return company, owner


def entries_for(company, table: str | None = None):
    query = AuditLog.objects.filter(company_id=company.id)
    if table:
        query = query.filter(table_name=table)
    return list(query.order_by("created_at"))


class TestWhatIsRecorded:
    def test_a_create_is_recorded_with_the_new_values(self, firm):
        company, owner = firm
        with company_context(company.id):
            Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="New customer"
            )

        entry = entries_for(company, "customer")[-1]
        assert entry.action == AuditAction.CREATE
        assert entry.new_values["legal_name"] == "New customer"

    def test_an_update_records_only_what_moved(self, firm):
        company, owner = firm
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Before"
            )
            reloaded = Customer.objects.get(pk=customer.pk)
            reloaded.legal_name = "After"
            reloaded.save()

        entry = entries_for(company, "customer")[-1]
        assert entry.action == AuditAction.UPDATE
        assert entry.old_values["legal_name"] == "Before"
        assert entry.new_values["legal_name"] == "After"
        # A diff that repeated every column would bury the one that changed.
        assert "type" not in entry.new_values

    def test_a_soft_delete_reads_as_a_deletion(self, firm):
        company, owner = firm
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Doomed"
            )
            Customer.objects.get(pk=customer.pk).delete()

        # At the database level it is an UPDATE, but to anyone reading the trail
        # a year later it was a deletion.
        assert entries_for(company, "customer")[-1].action == AuditAction.DELETE

    def test_saving_without_changing_anything_is_not_an_event(self, firm):
        company, owner = firm
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Static"
            )
            before = len(entries_for(company, "customer"))
            Customer.objects.get(pk=customer.pk).save()

        assert len(entries_for(company, "customer")) == before

    def test_the_actor_and_their_address_are_recorded(self, firm):
        company, owner = firm
        with (
            company_context(company.id),
            actor_context(RequestActor(user_id=owner.pk, ip_address="88.241.16.204")),
        ):
            Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Attributed"
            )

        entry = entries_for(company, "customer")[-1]
        assert entry.user_id == owner.pk
        assert entry.ip_address == "88.241.16.204"

    def test_writes_through_the_api_are_attributed(self, firm):
        company, owner = firm
        api_for(owner).post(
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "Via the API"},
        )
        entry = entries_for(company, "customer")[-1]
        # Taken from the token in middleware: a signal handler has no request to
        # ask, and threading it down by hand would touch every function between.
        assert entry.user_id == owner.pk


class TestMasking:
    def test_a_national_id_never_reaches_the_trail(self, firm):
        company, owner = firm
        with company_context(company.id):
            Customer.objects.create(
                company=company,
                type=CustomerType.INDIVIDUAL,
                legal_name="Person",
                national_id=TCKN,
            )

        entry = entries_for(company, "customer")[-1]
        assert entry.new_values["national_id"] == MASK
        # Belt and braces: the value must not appear anywhere in the row, in any
        # form, including the ciphertext.
        assert TCKN not in json.dumps(entry.new_values)

    def test_a_password_hash_never_reaches_the_trail(self, firm):
        company, owner = firm
        entry = next((row for row in entries_for(company, "user") if row.new_values), None)
        assert entry is not None
        assert entry.new_values["password"] == MASK

    def test_a_qr_token_is_masked(self, firm):
        from apps.elevators.models import Elevator
        from apps.elevators.services import assign_qr_token
        from apps.properties.models import Building, BuildingType

        company, owner = firm
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Owner of building"
            )
            building = Building.objects.create(
                company=company,
                customer=customer,
                name="A Blok",
                type=BuildingType.RESIDENTIAL,
                address_note="Test",
            )
            elevator = Elevator(company=company, building=building, name="Left")
            token = assign_qr_token(elevator)
            elevator.save()

        entry = entries_for(company, "elevator")[-1]
        # The token is what a QR sticker resolves to. In the trail it is a
        # credential, not a field worth keeping.
        assert entry.new_values["qr_token"] == MASK
        assert token not in json.dumps(entry.new_values)


class TestScope:
    def test_the_trail_does_not_audit_itself(self, firm):
        company, owner = firm
        with company_context(company.id):
            Customer.objects.create(company=company, type=CustomerType.CORPORATE, legal_name="Any")
        assert not AuditLog.objects.filter(table_name="audit_log").exists()

    def test_sessions_and_tokens_are_not_recorded(self, firm):
        company, _ = firm
        for table in ("refresh_session", "one_time_token"):
            assert not AuditLog.objects.filter(table_name=table).exists()

    def test_reference_data_is_not_recorded(self, db):
        from django.core.management import call_command

        call_command("load_address_data")
        # bulk_create does not fire signals, which is the correct outcome here:
        # auditing 50,000 unchanged rows every year would bury the entries that
        # matter.
        for table in ("province", "district", "neighborhood"):
            assert not AuditLog.objects.filter(table_name=table).exists()

    def test_each_entry_belongs_to_one_company(self, firm):
        company, _ = firm
        with system_context():
            from apps.companies.models import Company

            other = Company.objects.create(legal_name="Other Ltd", display_name="Other")
        with company_context(other.id):
            Customer.objects.create(company=other, type=CustomerType.CORPORATE, legal_name="Theirs")

        ours = entries_for(company, "customer")
        assert all(entry.company_id == company.id for entry in ours)
