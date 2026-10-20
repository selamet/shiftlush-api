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
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import pytest
from django.apps import apps
from django.core.paginator import UnorderedObjectListWarning
from django.urls import URLResolver, get_resolver, reverse
from rest_framework.filters import OrderingFilter
from rest_framework.request import Request
from rest_framework.settings import api_settings
from rest_framework.test import APIClient, APIRequestFactory

from apps.contracts.models import Contract, PricingType, Scope
from apps.customers.api.v1.views import CustomerViewSet
from apps.customers.models import Customer, CustomerType
from apps.properties.models import Building, BuildingType, Complex
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from core.filters import StableOrderingFilter

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

    def test_customers_with_the_same_legal_name(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            for _ in range(3):
                Customer.objects.create(
                    company=company, type=CustomerType.CORPORATE, legal_name="Same Name"
                )

        assert_list_is_stably_ordered(api_for(owner), reverse("customer-list"))


def _ends_in_primary_key(ordering: Sequence[str]) -> bool:
    return bool(ordering) and ordering[-1].lstrip("-") in {"id", "pk"}


class TestEveryOrderingHasATiebreaker:
    """#80: the fix above, held for every model and viewset, not just three.

    A new model or viewset that orders by a non-unique field fails here, before
    any list endpoint gets the chance to page through it inconsistently.
    """

    def test_every_model_ordering_ends_in_the_primary_key(self):
        offenders = [
            f"{model._meta.label}: {model._meta.ordering}"
            for model in apps.get_models()
            if model.__module__.startswith(("apps.", "core."))
            and model._meta.ordering
            and not _ends_in_primary_key([str(field) for field in model._meta.ordering])
        ]
        assert offenders == []

    def test_every_viewset_ordering_ends_in_the_primary_key(self):
        offenders = []
        for view in _routed_views():
            ordering = getattr(view, "ordering", None)
            if ordering and not _ends_in_primary_key(ordering):
                offenders.append(f"{view.__module__}.{view.__qualname__}: {ordering}")
        assert offenders == []


def _routed_views(patterns: Sequence[Any] | None = None) -> set[type]:
    views: set[type] = set()
    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(pattern, URLResolver):
            views |= _routed_views(pattern.url_patterns)
            continue
        view = getattr(pattern.callback, "cls", None)
        if view is not None:
            views.add(view)
    return views


class TestAClientChosenOrderingKeepsTheTiebreaker:
    def test_it_is_the_filter_every_list_endpoint_uses(self):
        # The page test below cannot prove this on SQLite, which hands tied
        # rows back in insertion order whatever the ORDER BY says.
        backends: list[Any] = list(api_settings.DEFAULT_FILTER_BACKENDS)
        assert StableOrderingFilter in backends
        assert OrderingFilter not in backends

    def _ordering_for(self, query: str) -> Any:
        request = Request(APIRequestFactory().get(f"/customers/{query}"))
        view = CustomerViewSet()
        return StableOrderingFilter().get_ordering(request, Customer.objects.none(), view)

    def test_the_primary_key_is_appended_to_the_clients_choice(self):
        assert self._ordering_for("?ordering=-created_at") == ["-created_at", "pk"]

    def test_a_field_the_view_does_not_allow_is_dropped_before_the_key_is_added(self):
        # `id` is not in CustomerViewSet.ordering_fields, so DRF discards it and
        # the tiebreaker is appended to what survives.
        assert self._ordering_for("?ordering=legal_name,-id") == ["legal_name", "pk"]

    def test_the_views_default_is_used_when_the_client_asks_for_nothing(self):
        assert self._ordering_for("") == ["legal_name", "id"]

    def test_tied_rows_come_back_in_the_same_order_on_every_page(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            for _ in range(5):
                Customer.objects.create(
                    company=company, type=CustomerType.CORPORATE, legal_name="Same Name"
                )
        client = api_for(owner)
        url = reverse("customer-list")

        def page(n: int) -> list[str]:
            response = client.get(url, {"ordering": "legal_name", "page_size": "2", "page": str(n)})
            return [row["id"] for row in response.json()["results"]]

        seen = page(1) + page(2) + page(3)
        assert len(seen) == len(set(seen)) == 6
