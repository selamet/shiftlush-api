"""The customer endpoints, and the two boundaries they have to hold.

The unit tests proved the manager filters and the matrix answers correctly.
This proves the same things through HTTP, which is the only place it counts —
a boundary that holds in a unit test and leaks through a viewset is no boundary.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


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


@pytest.fixture
def firm_b(db):
    company, owner = register_company(
        legal_name="Firm B Ltd",
        display_name="Firm B",
        first_name="B",
        last_name="Owner",
        email="b@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Customer of B"
        )
    return company, owner, customer


def make_user(company, role: str, email: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name="X",
            last_name="Y",
            role=role,
        )


class TestTenantIsolationOverHttp:
    def test_list_shows_only_your_own(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        response = api_for(owner_a).get(reverse("customer-list"))
        assert [row["legal_name"] for row in response.data["results"]] == ["Customer of A"]

    def test_fetching_another_firms_record_is_a_404(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, _, customer_b = firm_b
        response = api_for(owner_a).get(reverse("customer-detail", args=[customer_b.pk]))
        # 404 rather than 403: a 403 confirms the record exists, which lets an
        # outsider probe for valid ids and count a competitor's records.
        assert response.status_code == 404

    def test_patching_another_firms_record_is_a_404(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, _, customer_b = firm_b
        response = api_for(owner_a).patch(
            reverse("customer-detail", args=[customer_b.pk]), {"legal_name": "Renamed"}
        )
        assert response.status_code == 404
        with system_context():
            assert Customer.unscoped.get(pk=customer_b.pk).legal_name == "Customer of B"

    def test_the_company_cannot_be_chosen_by_the_client(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        company_b, _, _ = firm_b
        response = api_for(owner_a).post(
            reverse("customer-list"),
            {
                "type": CustomerType.CORPORATE,
                "legal_name": "Smuggled",
                "company": str(company_b.pk),
            },
        )
        # `company` is not a writable field, so naming it is a client error.
        assert response.status_code == 400


class TestRoleBoundaries:
    def test_operations_may_write(self, firm_a):
        company, _, _ = firm_a
        user = make_user(company, Role.OPERATIONS, "ops@example.com")
        response = api_for(user).post(
            reverse("customer-list"), {"type": CustomerType.CORPORATE, "legal_name": "New"}
        )
        assert response.status_code == 201

    def test_technician_reads_but_cannot_write(self, firm_a):
        company, _, _ = firm_a
        user = make_user(company, Role.TECHNICIAN, "tech@example.com")
        client = api_for(user)
        assert client.get(reverse("customer-list")).status_code == 200
        assert (
            client.post(
                reverse("customer-list"), {"type": CustomerType.CORPORATE, "legal_name": "Nope"}
            ).status_code
            == 403
        )

    def test_technician_with_no_assignment_sees_an_empty_list(self, firm_a):
        company, _, _ = firm_a
        user = make_user(company, Role.TECHNICIAN, "tech2@example.com")
        response = api_for(user).get(reverse("customer-list"))
        # Nothing has gone wrong: they simply have no work yet. An error here
        # would send them to support over a normal state.
        assert response.status_code == 200
        assert response.data["results"] == []

    def test_technician_sees_assigned_customers_only(self, firm_a):
        company, _, customer = firm_a
        user = make_user(company, Role.TECHNICIAN, "tech3@example.com")
        with company_context(company.id):
            Customer.objects.create(
                company=company, type=CustomerType.PUBLIC, legal_name="Not assigned"
            )
            user.customer_assignments.create(company=company, customer=customer)

        response = api_for(user).get(reverse("customer-list"))
        assert [row["legal_name"] for row in response.data["results"]] == ["Customer of A"]

    def test_anonymous_is_refused(self, firm_a):
        assert APIClient().get(reverse("customer-list")).status_code == 401


class TestValidation:
    def test_bad_tax_number_is_named(self, firm_a):
        _, owner, _ = firm_a
        response = api_for(owner).post(
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "Bad tax", "tax_number": "1111111111"},
        )
        assert response.status_code == 400
        assert response.data["error"]["details"][0]["code"] == "INVALID_TAX_NUMBER"

    def test_phone_is_folded_to_one_form(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "Phone", "phone": "0555 123 45 67"},
        )
        assert created.data["phone"] == "+905551234567"

    def test_national_id_is_never_returned(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(
            reverse("customer-list"),
            {
                "type": CustomerType.INDIVIDUAL,
                "legal_name": "Person",
                "national_id": "10000000146",
            },
        )
        assert created.status_code == 201
        # Personal data, encrypted at rest, and on no screen — so it stays off
        # the response entirely rather than being sent to every client that
        # renders a table.
        assert "national_id" not in created.data
        detail = client.get(reverse("customer-detail", args=[created.data["id"]]))
        assert "national_id" not in detail.data


class TestDeletion:
    def test_delete_is_soft(self, firm_a):
        _, owner, customer = firm_a
        assert (
            api_for(owner).delete(reverse("customer-detail", args=[customer.pk])).status_code == 204
        )
        with system_context():
            assert Customer.unscoped.get(pk=customer.pk).is_deleted

    def test_a_customer_with_buildings_cannot_be_deleted(self, firm_a):
        from apps.properties.models import Building, BuildingType

        company, owner, customer = firm_a
        with company_context(company.id):
            Building.objects.create(
                company=company,
                customer=customer,
                name="A Blok",
                type=BuildingType.RESIDENTIAL,
                address_note="Test",
            )

        response = api_for(owner).delete(reverse("customer-detail", args=[customer.pk]))
        # PROTECT never fires on a soft delete, so this rule lives in the view.
        # Without it the customer would vanish from under its buildings.
        assert response.status_code == 409
        assert response.data["error"]["code"] == "RECORD_IN_USE"
