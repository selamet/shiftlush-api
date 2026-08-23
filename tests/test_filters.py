"""Filtering by a foreign key.

Every list screen filters by a related record — a customer's buildings, a
building's elevators — and none of it worked. django-filter derives a
ModelChoiceFilter from a foreign key and evaluates its queryset when the class
is defined: at start-up, with no request, where the tenant manager correctly
returns nothing. The filter was then born with an empty set of choices and
answered `invalid_choice` to every id it was ever given.

No existing test filtered by a foreign key, which is why it survived. These do.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import django_filters
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.contracts.models import Contract, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


@pytest.fixture
def firm(db):
    from datetime import date, timedelta

    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        wanted = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Wanted"
        )
        other = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Other"
        )
        for customer in (wanted, other):
            building = Building.objects.create(
                company=company,
                customer=customer,
                name=f"{customer.legal_name} Blok",
                type=BuildingType.RESIDENTIAL,
                address_note="Test",
            )
            elevator = Elevator(company=company, building=building, name="Left")
            # The token is unique without a soft-delete condition, so two
            # elevators saved without one collide on the empty string.
            assign_qr_token(elevator)
            elevator.save()
            Contract.objects.create(
                company=company,
                customer=customer,
                contract_number=f"{customer.legal_name}-1",
                scope=Scope.MAINTENANCE_ONLY,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=365),
                pricing_type=PricingType.FLAT,
                monthly_fee="1000.00",
            )
    return company, owner, wanted, other


def results(response) -> list:
    return response.data["results"]


class TestFilteringByRelation:
    def test_buildings_by_customer(self, firm):
        _, owner, wanted, _ = firm
        response = api_for(owner).get(reverse("building-list"), {"customer": str(wanted.id)})

        assert response.status_code == 200
        assert [row["customer_name"] for row in results(response)] == ["Wanted"]

    def test_contracts_by_customer(self, firm):
        _, owner, wanted, _ = firm
        response = api_for(owner).get(reverse("contract-list"), {"customer": str(wanted.id)})

        assert response.status_code == 200
        assert [row["customer_name"] for row in results(response)] == ["Wanted"]

    def test_elevators_by_building(self, firm):
        company, owner, wanted, _ = firm
        with company_context(company.id):
            building = Building.objects.get(customer=wanted)

        response = api_for(owner).get(reverse("elevator-list"), {"building": str(building.id)})

        assert response.status_code == 200
        assert len(results(response)) == 1

    def test_elevators_by_customer(self, firm):
        _, owner, wanted, _ = firm
        response = api_for(owner).get(reverse("elevator-list"), {"customer": str(wanted.id)})
        assert len(results(response)) == 1


class TestWhatAnUnknownIdDoes:
    def test_an_id_that_exists_nowhere_returns_an_empty_list(self, firm):
        _, owner, _, _ = firm
        response = api_for(owner).get(reverse("building-list"), {"customer": str(uuid.uuid4())})

        # Not a 400. Nothing about the request is malformed: the caller asked
        # for the buildings of a customer they cannot see, and the answer is
        # that there are none.
        assert response.status_code == 200
        assert results(response) == []

    def test_another_companys_id_looks_exactly_the_same(self, firm):
        _, owner, _, _ = firm
        with system_context():
            other_company, _ = register_company(
                legal_name="Other Ltd",
                display_name="Other",
                first_name="O",
                last_name="Ther",
                email="stranger@example.com",
                password=PASSWORD,
            )
            theirs = Customer.objects.create(
                company=other_company, type=CustomerType.CORPORATE, legal_name="Theirs"
            )

        response = api_for(owner).get(reverse("building-list"), {"customer": str(theirs.id)})

        # Identical to the unknown-id case above, and that is the point: a
        # different answer for "exists but not yours" would let an outsider
        # confirm which ids are real.
        assert response.status_code == 200
        assert results(response) == []

    def test_a_malformed_id_is_still_rejected(self, firm):
        _, owner, _, _ = firm
        response = api_for(owner).get(reverse("building-list"), {"customer": "not-a-uuid"})
        # This one *is* malformed, so 400 is right. Accepting it silently would
        # hide a client bug behind an empty list.
        assert response.status_code == 400


class TestTheTrapItself:
    def test_no_filter_binds_a_queryset_at_class_definition(self):
        """The guard, so this cannot come back through a new Meta.fields entry.

        A queryset built when the class is defined is evaluated outside any
        request. For a tenant-scoped model that means empty, permanently, and
        the symptom is a filter that rejects every value it is given.
        """
        import importlib

        modules = [
            "apps.properties.api.v1.views",
            "apps.contracts.api.v1.views",
            "apps.elevators.api.v1.views",
            "apps.customers.api.v1.views",
            "apps.users.api.v1.team_views",
            "apps.attachments.api.v1.views",
            "apps.audit.api.v1.views",
            "apps.address.api.v1.views",
        ]

        offenders = []
        for name in modules:
            module = importlib.import_module(name)
            for attribute, candidate in vars(module).items():
                if not inspect.isclass(candidate):
                    continue
                if not issubclass(candidate, django_filters.FilterSet):
                    continue
                if candidate is django_filters.FilterSet:
                    continue
                declared_filters: dict[str, Any] = getattr(candidate, "base_filters", {})
                for field, declared in declared_filters.items():
                    queryset = getattr(declared, "queryset", None)
                    if queryset is not None and not callable(queryset):
                        offenders.append(f"{attribute}.{field}")

        assert offenders == [], (
            f"These filters bind a queryset at import time: {offenders}. "
            f"Declare them as UUIDFilter on the raw column instead."
        )
