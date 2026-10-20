"""Who a maintenance visit can be recorded against.

`technician` arrives from the client, and `User` is the one model whose default
manager is not tenant-scoped (authentication has to find a user before it knows
the company). So the field has to be scoped by hand, or a visit could name a
user from another firm — and its read response would hand back that user's name.

A technician records their own visits. They may leave the field out, and may not
name somebody else: a visit is a signed statement that the named person did the
work.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.urls import reverse
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from apps.customers.models import Customer
from apps.maintenance.api.v1.serializers import MaintenanceVisitWriteSerializer
from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context
from core.error_codes import ErrorCode
from tests import test_maintenance_visits
from tests.test_pagination_ordering import _routed_views

# The shared firm, elevator and active contract, reused rather than rebuilt.
visit_fixture = test_maintenance_visits.visit_fixture
PASSWORD = test_maintenance_visits.PASSWORD


def api_for(user: User) -> APIClient:
    # A fresh key per client: visit creates require one (M1), and each test
    # builds a new client per request.
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    return client


def make_user(company: Any, email: str, role: str) -> User:
    with company_context(company.id):
        user = User.objects.create_user(
            company=company,
            email=email,
            password=PASSWORD,
            first_name=email.split("@")[0],
            last_name="X",
            role=role,
        )
        if role == Role.TECHNICIAN:
            user.customer_assignments.create(
                company=company, customer=Customer.objects.get(legal_name="Cust")
            )
    return user


def visit_body(elevator: Any, contract: Any, **extra: Any) -> dict[str, Any]:
    return {
        "elevator": str(elevator.id),
        "contract": str(contract.id),
        "visit_date": str(date.today()),
        "status": VisitStatus.COMPLETED,
        **extra,
    }


@pytest.fixture
def stranger(db) -> User:
    _, owner = register_company(
        legal_name="Other Firm Ltd",
        display_name="Other",
        first_name="Somebody",
        last_name="Else",
        email="owner@other.example.com",
        password=PASSWORD,
    )
    return owner


class TestTheTechnicianBelongsToTheFirm:
    def test_a_user_from_another_firm_is_refused_as_if_they_did_not_exist(
        self, visit_fixture, stranger
    ):
        _, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(stranger.id)),
        )

        assert response.status_code == 400, response.data
        assert "Somebody" not in str(response.content)
        assert not MaintenanceVisit.unscoped.exists()

    def test_nor_can_an_edit_move_a_visit_to_one(self, visit_fixture, stranger):
        company, owner, elevator, contract = visit_fixture
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)
        created = api_for(owner).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(technician.id)),
        )
        assert created.status_code == 201, created.data

        response = api_for(owner).patch(
            reverse("maintenance-visit-detail", args=[created.data["id"]]),
            {"technician": str(stranger.id)},
        )

        assert response.status_code == 400, response.data
        assert MaintenanceVisit.unscoped.get().technician_id == technician.id

    def test_a_deactivated_colleague_is_refused(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        former = make_user(company, "former@example.com", Role.TECHNICIAN)
        former.is_active = False
        former.save(update_fields=["is_active"])

        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(former.id)),
        )
        assert response.status_code == 400, response.data

    def test_the_office_can_record_a_visit_for_a_colleague(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)

        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(technician.id)),
        )
        assert response.status_code == 201, response.data
        assert response.data["technician"] == technician.id

    def test_the_office_has_to_say_who_did_it(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"), visit_body(elevator, contract)
        )
        assert response.status_code == 400, response.data
        assert "technician" in response.data["error"]["details"][0]["field"]


class TestATechnicianRecordsTheirOwnVisits:
    def test_leaving_it_out_records_the_visit_as_theirs(self, visit_fixture):
        company, _, elevator, contract = visit_fixture
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)

        response = api_for(technician).post(
            reverse("maintenance-visit-list"), visit_body(elevator, contract)
        )

        assert response.status_code == 201, response.data
        assert response.data["technician"] == technician.id

    def test_naming_themselves_is_fine(self, visit_fixture):
        company, _, elevator, contract = visit_fixture
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)

        response = api_for(technician).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(technician.id)),
        )
        assert response.status_code == 201, response.data

    def test_naming_a_colleague_is_refused(self, visit_fixture):
        company, _, elevator, contract = visit_fixture
        technician = make_user(company, "tech@example.com", Role.TECHNICIAN)
        colleague = make_user(company, "colleague@example.com", Role.TECHNICIAN)

        response = api_for(technician).post(
            reverse("maintenance-visit-list"),
            visit_body(elevator, contract, technician=str(colleague.id)),
        )

        assert response.status_code == 400, response.data
        details = response.data["error"]["details"]
        assert details == [
            {"field": "technician", "code": ErrorCode.TECHNICIAN_RECORDS_OWN_VISITS.value}
        ]
        assert not MaintenanceVisit.unscoped.exists()


def _writable_user_fields() -> list[tuple[type, str]]:
    found = []
    for view in _routed_views():
        serializer = getattr(view, "write_serializer_class", None) or getattr(
            view, "serializer_class", None
        )
        meta = getattr(serializer, "Meta", None)
        names = getattr(meta, "fields", None)
        if serializer is None or not isinstance(names, list | tuple):
            continue
        read_only = set(getattr(meta, "read_only_fields", ()) or ())
        for name in names:
            try:
                field = meta.model._meta.get_field(name)  # type: ignore[union-attr]
            except FieldDoesNotExist:
                continue
            if field.related_model is User and name not in read_only:
                found.append((serializer, name))
    return found


class TestEveryWritableUserFieldIsScopedToTheFirm:
    """The rule above, for whatever serializer gains a user field next.

    Every other model's manager scopes a foreign key by itself; User's does not.
    """

    def test_there_is_at_least_the_one_this_file_is_about(self):
        assert (MaintenanceVisitWriteSerializer, "technician") in _writable_user_fields()

    def test_none_of_them_offers_a_user_from_another_firm(self, visit_fixture, stranger):
        _, owner, _, _ = visit_fixture
        request = Request(APIRequestFactory().post("/"))
        request.user = owner
        for serializer, name in _writable_user_fields():
            field = serializer(context={"request": request}).fields[name]
            offered = set(field.get_queryset().values_list("company_id", flat=True))
            assert offered <= {owner.company_id}, f"{serializer.__name__}.{name}"
