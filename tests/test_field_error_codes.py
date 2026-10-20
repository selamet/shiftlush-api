"""A business rule reported on a field arrives with its own code.

DRF takes a field error's code from the ErrorDetail, not from its text. Writing
`{"end_date": ErrorCode.END_DATE_BEFORE_START_DATE.value}` puts the code in the
message and leaves the code as `invalid` — so the client, which maps codes to
sentences, showed "invalid value" instead of saying the end date was before the
start date. These pin the wire format for the three rules that did that.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.properties.models import Building, BuildingType, Complex
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from core.error_codes import ErrorCode

PASSWORD = "correct-horse-battery"
TODAY = date.today()


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
        other = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Other Customer"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="B1",
            type=BuildingType.RESIDENTIAL,
            address_note="x",
        )
        elevator = Elevator.objects.create(company=company, building=building, qr_token="c" * 24)
        foreign_complex = Complex.objects.create(company=company, customer=other, name="Site")
    return owner, customer, building, elevator, foreign_complex


def details(response) -> list[dict[str, str]]:
    assert response.status_code == 400, response.data
    return response.data["error"]["details"]


class TestTheRuleIsTheCode:
    def test_a_contract_ending_before_it_starts(self, firm):
        owner, customer, *_ = firm
        response = api_for(owner).post(
            reverse("contract-list"),
            {
                "customer": str(customer.id),
                "contract_number": "C-1",
                "scope": "maintenance_only",
                "start_date": str(TODAY),
                "end_date": str(TODAY - timedelta(days=1)),
                "pricing_type": "flat",
                "monthly_fee": "1000.00",
                "vat_rate": "20.00",
            },
        )
        assert details(response) == [
            {"field": "end_date", "code": ErrorCode.END_DATE_BEFORE_START_DATE.value}
        ]

    def test_an_inspection_due_before_the_last_one(self, firm):
        owner, _, _, elevator, _ = firm
        response = api_for(owner).patch(
            reverse("elevator-detail", args=[elevator.id]),
            {
                "last_inspection_date": str(TODAY),
                "next_inspection_date": str(TODAY - timedelta(days=1)),
            },
        )
        assert details(response) == [
            {
                "field": "next_inspection_date",
                "code": ErrorCode.END_DATE_BEFORE_START_DATE.value,
            }
        ]

    def test_a_building_under_another_customers_complex(self, firm):
        owner, _, building, _, foreign_complex = firm
        response = api_for(owner).patch(
            reverse("building-detail", args=[building.id]),
            {"complex": str(foreign_complex.id)},
        )
        assert details(response) == [
            {"field": "complex", "code": ErrorCode.BUILDING_CUSTOMER_MISMATCH.value}
        ]


class TestNoSerializerPutsTheCodeInTheMessage:
    """The pattern itself, so the next rule written this way fails here."""

    def test_no_error_dict_value_is_an_error_code_string(self):
        offenders = []
        for path in sorted(Path("apps").rglob("serializers.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Dict):
                    continue
                for value in node.values:
                    # ErrorCode.SOMETHING.value used as the dict value
                    if (
                        isinstance(value, ast.Attribute)
                        and value.attr == "value"
                        and isinstance(value.value, ast.Attribute)
                        and isinstance(value.value.value, ast.Name)
                        and value.value.value.id == "ErrorCode"
                    ):
                        offenders.append(f"{path}:{value.lineno}")
        assert offenders == []
