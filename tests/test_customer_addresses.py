"""The customer address book: the model and its single billing slot."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.customers.models import Customer, CustomerAddress, CustomerType
from apps.customers.services import demote_other_billing
from apps.users.services import register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"


@pytest.fixture
def firm_a(db):
    company, owner = register_company(
        legal_name="Firm A Ltd",
        display_name="Firm A",
        first_name="A",
        last_name="Owner",
        email="a@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Customer of A"
        )
    return company, owner, customer


class TestTheBillingSlot:
    def test_two_live_billing_addresses_are_refused_by_the_database(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="A", is_billing=True
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                CustomerAddress.objects.create(
                    company=company, customer=customer, label="Two", street="B", is_billing=True
                )

    def test_a_deleted_billing_address_frees_the_slot(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            first = CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="A", is_billing=True
            )
            first.delete()  # soft delete
            CustomerAddress.objects.create(
                company=company, customer=customer, label="Two", street="B", is_billing=True
            )

    def test_demote_other_billing_clears_every_other_holder(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            old = CustomerAddress.objects.create(
                company=company, customer=customer, label="Old", street="A", is_billing=True
            )
            demote_other_billing(customer)
            old.refresh_from_db()
            assert old.is_billing is False

    def test_billing_sorts_first_then_label(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            for label, billing in [("B", False), ("A", False), ("Z", True)]:
                CustomerAddress.objects.create(
                    company=company, customer=customer, label=label, street="x", is_billing=billing
                )
            assert [a.label for a in customer.addresses.all()] == ["Z", "A", "B"]
