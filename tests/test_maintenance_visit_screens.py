"""What the web app's visit screens need from the API, found while building them.

M1 (#86): the elevator detail could not show the maintenance dates SL-119
computes, the contract described the create response without its `id`, an
operations user could create a visit but not name the technician who did it,
and the visit list could not be narrowed to a date range.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from django.urls import reverse
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from apps.customers.models import Customer
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from tests import test_maintenance_visits

visit_fixture = test_maintenance_visits.visit_fixture
PASSWORD = test_maintenance_visits.PASSWORD


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    return client


def make_user(company: Any, email: str, role: str, **extra: Any) -> User:
    with company_context(company.id):
        user = User.objects.create_user(
            company=company,
            email=email,
            password=PASSWORD,
            first_name=email.split("@")[0].title(),
            last_name="X",
            role=role,
            **extra,
        )
        if role == Role.TECHNICIAN:
            user.customer_assignments.create(
                company=company, customer=Customer.objects.get(legal_name="Cust")
            )
    return user


def record_visit(owner: User, elevator: Any, contract: Any, day: date) -> Any:
    response = api_for(owner).post(
        reverse("maintenance-visit-list"),
        {
            "elevator": str(elevator.id),
            "contract": str(contract.id),
            "technician": str(owner.id),
            "visit_date": str(day),
            "status": VisitStatus.COMPLETED,
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    return response


class TestTheElevatorShowsItsMaintenanceDates:
    def test_they_are_on_the_detail_response(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        record_visit(owner, elevator, contract, date.today() - timedelta(days=3))

        detail = api_for(owner).get(reverse("elevator-detail", args=[elevator.id])).data

        assert detail["last_maintenance_date"] == str(date.today() - timedelta(days=3))
        assert detail["next_maintenance_date"] == str(date.today() + timedelta(days=27))

    def test_they_are_null_before_any_visit(self, visit_fixture):
        _, owner, elevator, _ = visit_fixture
        detail = api_for(owner).get(reverse("elevator-detail", args=[elevator.id])).data
        assert detail["last_maintenance_date"] is None
        assert detail["next_maintenance_date"] is None

    def test_a_client_cannot_set_them(self, visit_fixture):
        # Derived from visits. A client writing them would be overwritten by
        # the next visit, and until then would be showing an invented date.
        _, owner, elevator, _ = visit_fixture
        api_for(owner).patch(
            reverse("elevator-detail", args=[elevator.id]),
            {"next_maintenance_date": "2030-01-01"},
            format="json",
        )
        assert Elevator.unscoped.get(id=elevator.id).next_maintenance_date is None


class TestTheContractDescribesWhatCreateReturns:
    def test_the_201_is_the_read_shape_with_an_id(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        created = schema["paths"]["/api/v1/maintenance-visits/"]["post"]["responses"]["201"]
        ref = created["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/MaintenanceVisitRead")


class TestTheOfficeCanNameATechnician:
    def test_operations_sees_the_firms_active_technicians_and_nobody_else(self, visit_fixture):
        company, _, _, _ = visit_fixture
        operations = make_user(company, "ops@example.com", Role.OPERATIONS)
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)
        former = make_user(company, "former@example.com", Role.TECHNICIAN, is_active=False)
        register_company(
            legal_name="Other Ltd",
            display_name="Other",
            first_name="Other",
            last_name="Owner",
            email="owner@other.example.com",
            password=PASSWORD,
        )

        response = api_for(operations).get(reverse("maintenance-visit-technicians"))

        assert response.status_code == 200, response.data
        assert response.data == [{"id": str(technician.id), "name": f"{technician.first_name} X"}]
        assert str(former.id) not in {row["id"] for row in response.data}

    def test_it_carries_names_only(self, visit_fixture):
        # Operations may not read the user list (e-mails, phones, roles); this
        # exists so they can fill in one field, and says no more than that.
        company, _, _, _ = visit_fixture
        operations = make_user(company, "ops@example.com", Role.OPERATIONS)
        make_user(company, "tech@example.com", Role.TECHNICIAN)

        row = api_for(operations).get(reverse("maintenance-visit-technicians")).data[0]

        assert set(row) == {"id", "name"}

    def test_an_accountant_may_not_read_it(self, visit_fixture):
        company, _, _, _ = visit_fixture
        accountant = make_user(company, "acc@example.com", Role.ACCOUNTANT)
        response = api_for(accountant).get(reverse("maintenance-visit-technicians"))
        assert response.status_code == 403


class TestTheListNarrowsToADateRange:
    def test_after_and_before_are_inclusive(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        today = date.today()
        for days_ago in (10, 5, 1):
            record_visit(owner, elevator, contract, today - timedelta(days=days_ago))

        response = api_for(owner).get(
            reverse("maintenance-visit-list"),
            {
                "visit_date_after": str(today - timedelta(days=5)),
                "visit_date_before": str(today - timedelta(days=1)),
            },
        )

        assert response.status_code == 200
        dates = sorted(row["visit_date"] for row in response.data["results"])
        assert dates == [str(today - timedelta(days=5)), str(today - timedelta(days=1))]
        assert MaintenanceVisit.unscoped.count() == 3
