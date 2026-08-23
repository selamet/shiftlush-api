"""The N+1 guard, tested on itself — specification 8.8.

A check nobody has ever seen fail is indistinguishable from a check that
inspects nothing. `tests/conftest.py` closes the database while a list is being
serialised; these tests make it fail on purpose, so that the guard going quiet
is itself a failure.

The last test is a plain query count on the contract list. It restates in
numbers what the guard says in exceptions, and it survives the guard being
disabled — the contract list *was* one query per contract plus two per line
until the guard found it, and this is the test that would catch it coming back.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers
from rest_framework.test import APIClient
from zen_queries import QueriesDisabledError

from apps.contracts.models import Contract, ContractElevator, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"
TODAY = date.today()


class BuildingWithCustomerName(serializers.ModelSerializer[Building]):
    """Walks one foreign key, which is all an N+1 ever is."""

    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)

    class Meta:
        model = Building
        fields: ClassVar[list[str]] = ["id", "customer_name"]


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
        for index in range(3):
            Building.objects.create(
                company=company,
                customer=customer,
                name=f"Block {index}",
                type=BuildingType.RESIDENTIAL,
                address_note="Test",
            )
    return company, owner, customer


class TestTheGuardItself:
    def test_it_refuses_a_list_that_reaches_for_a_missing_join(self, firm):
        company, _, _ = firm
        with company_context(company.id), pytest.raises(QueriesDisabledError):
            # No select_related, so rendering `customer_name` is one query per
            # row. That is the failure the guard exists to produce.
            _ = BuildingWithCustomerName(Building.objects.all(), many=True).data

    def test_it_says_nothing_when_the_join_is_declared(self, firm):
        company, _, _ = firm
        with company_context(company.id):
            rows = BuildingWithCustomerName(
                Building.objects.select_related("customer"), many=True
            ).data
        assert [row["customer_name"] for row in rows] == ["Customer"] * 3

    def test_a_single_record_is_left_alone(self, firm):
        """The guard is about lists, and only lists.

        A detail endpoint that walks a relation costs one query, not one per
        row. Refusing it here would be a different rule wearing this one's name.
        """
        company, _, _ = firm
        with company_context(company.id):
            building = Building.objects.first()
            assert BuildingWithCustomerName(building).data["customer_name"] == "Customer"


class TestTheContractListStaysFlat:
    """The list that was actually N+1 before the guard was added."""

    def _contract_with_lines(self, company, customer, number: str):
        with company_context(company.id):
            building = Building.objects.create(
                company=company,
                customer=customer,
                name=f"Block for {number}",
                type=BuildingType.RESIDENTIAL,
                address_note="Test",
            )
            contract = Contract.objects.create(
                company=company,
                customer=customer,
                contract_number=number,
                scope=Scope.MAINTENANCE_AND_REPAIR,
                start_date=TODAY,
                end_date=TODAY + timedelta(days=365),
                pricing_type=PricingType.PER_ELEVATOR,
                vat_rate="20.00",
            )
            for index in range(2):
                elevator = Elevator(
                    company=company,
                    building=building,
                    registration_number=f"34-2020-{number}-{index}",
                    name=f"Elevator {index}",
                )
                assign_qr_token(elevator)
                elevator.save()
                ContractElevator.objects.create(
                    company=company,
                    contract=contract,
                    elevator=elevator,
                    unit_price="1000.00",
                    added_at=TODAY,
                )

    def test_the_query_count_does_not_grow_with_the_rows(self, firm):
        company, owner, customer = firm
        client = api_for(owner)

        self._contract_with_lines(company, customer, "2026-0001")
        with CaptureQueriesContext(connection) as one_row:
            assert client.get("/api/v1/contracts/").status_code == 200

        for number in ("2026-0002", "2026-0003", "2026-0004"):
            self._contract_with_lines(company, customer, number)
        with CaptureQueriesContext(connection) as four_rows:
            assert client.get("/api/v1/contracts/").status_code == 200

        assert len(four_rows) == len(one_row), (
            f"The contract list costs {len(one_row)} queries for one contract and "
            f"{len(four_rows)} for four. A list whose cost depends on its length is "
            f"the N+1 in section 8.8."
        )
