"""Replay protection.

The scenario: a bad connection, a tap on save, nothing visibly happens, another
tap. Without this there are now two contracts, both looking legitimate.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contracts.models import Contract, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from core.models import IdempotencyKey

PASSWORD = "correct-horse-battery"
TODAY = date.today()
KEY = "9f1c0e2a-retry-once"


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
            company=company, type=CustomerType.CORPORATE, legal_name="A customer"
        )
    return company, owner, customer


def payload(customer_id) -> dict:
    return {
        "customer": str(customer_id),
        "scope": Scope.MAINTENANCE_ONLY,
        "start_date": str(TODAY),
        "end_date": str(TODAY + timedelta(days=365)),
        "pricing_type": PricingType.FLAT,
        "monthly_fee": "1000.00",
    }


class TestReplay:
    def test_the_same_key_creates_one_record(self, firm):
        company, owner, customer = firm
        client = api_for(owner)

        first = client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        second = client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        assert first.status_code == second.status_code == 201
        # The second call returns the first answer rather than making a second
        # contract, so the client cannot tell the retry happened.
        assert first.data["id"] == second.data["id"]
        with company_context(company.id):
            assert Contract.objects.count() == 1

    def test_a_different_key_creates_a_second_record(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        client.post(
            reverse("contract-list"),
            payload(customer.id),
            format="json",
            HTTP_IDEMPOTENCY_KEY="a-different-key",
        )
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_no_key_means_no_protection(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(reverse("contract-list"), payload(customer.id), format="json")
        client.post(reverse("contract-list"), payload(customer.id), format="json")
        # The header is optional; a client that does not send one gets the
        # behaviour it had before.
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_the_same_key_with_a_different_body_is_refused(self, firm):
        _, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        changed = payload(customer.id) | {"monthly_fee": "9999.00"}
        response = client.post(
            reverse("contract-list"), changed, format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        # Serving the stored answer here would reply to a question that was
        # never asked — one of the hardest bugs to trace afterwards.
        assert response.status_code == 409
        assert response.data["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_an_expired_key_no_longer_replays(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        IdempotencyKey.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_a_failed_request_is_not_stored(self, firm):
        _, owner, customer = firm
        client = api_for(owner)
        broken = payload(customer.id) | {"end_date": str(TODAY - timedelta(days=1))}

        assert (
            client.post(
                reverse("contract-list"), broken, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            ).status_code
            == 400
        )
        # Storing a failure would make a transient error permanent for a day.
        assert not IdempotencyKey.objects.filter(key=KEY).exists()

        assert (
            client.post(
                reverse("contract-list"),
                payload(customer.id),
                format="json",
                HTTP_IDEMPOTENCY_KEY=KEY,
            ).status_code
            == 201
        )

    def test_keys_do_not_cross_between_users(self, firm):
        from core.context import system_context

        company, owner, customer = firm
        with system_context():
            other = User.objects.create_user(
                email="other@example.com",
                password=PASSWORD,
                company=company,
                first_name="O",
                last_name="Ther",
                role=owner.role,
            )

        api_for(owner).post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        api_for(other).post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        # Two people can pick the same key by coincidence; one must not receive
        # the other's response.
        with company_context(company.id):
            assert Contract.objects.count() == 2
