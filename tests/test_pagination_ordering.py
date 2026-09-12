"""Deterministic pagination ordering — specification 8.8.

The complex, building and contract list endpoints annotate an aggregate count
onto their queryset (buildings per complex, elevators per building, and so
on), and Django does not carry a model's default `Meta.ordering` through a
GROUP BY query. Two rows that tie on the model's natural sort key — two
buildings named the same, two contracts started on the same day — can then
come back in a different order between pages. `UnorderedObjectListWarning`
says exactly this. Spec 8.8 requires a stable default ordering; a tie between
rows is not something the API should decide silently and inconsistently.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta

import pytest
from django.core.paginator import UnorderedObjectListWarning
from django.urls import reverse
from rest_framework.test import APIClient

from apps.contracts.models import Contract, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.properties.models import Building, BuildingType, Complex
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context

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
            company=company, type=CustomerType.CORPORATE, legal_name="Customer"
        )
    return company, owner, customer


def assert_list_is_stably_ordered(client: APIClient, url: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnorderedObjectListWarning)
        response = client.get(url)
    assert response.status_code == 200


class TestListsStayOrderedWhenRowsTieOnTheSortKey:
    def test_complexes_with_the_same_name(self, firm):
        company, owner, customer = firm
        with company_context(company.id):
            for _ in range(3):
                Complex.objects.create(company=company, customer=customer, name="Same Name")

        assert_list_is_stably_ordered(api_for(owner), reverse("complex-list"))

    def test_buildings_with_the_same_name(self, firm):
        company, owner, customer = firm
        with company_context(company.id):
            for _ in range(3):
                Building.objects.create(
                    company=company,
                    customer=customer,
                    name="Same Name",
                    type=BuildingType.RESIDENTIAL,
                    address_note="Test",
                )

        assert_list_is_stably_ordered(api_for(owner), reverse("building-list"))

    def test_contracts_with_the_same_start_date(self, firm):
        company, owner, customer = firm
        same_day = date.today()
        with company_context(company.id):
            for i in range(3):
                Contract.objects.create(
                    company=company,
                    customer=customer,
                    contract_number=f"C-{i}",
                    scope=Scope.MAINTENANCE_ONLY,
                    start_date=same_day,
                    end_date=same_day + timedelta(days=365),
                    pricing_type=PricingType.FLAT,
                    monthly_fee="1000.00",
                )

        assert_list_is_stably_ordered(api_for(owner), reverse("contract-list"))
