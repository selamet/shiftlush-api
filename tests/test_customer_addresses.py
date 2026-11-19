"""The customer address book: the model, its billing slot and its endpoints."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerAddress, CustomerType
from apps.customers.services import demote_other_billing
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


def nested(customer) -> str:
    return reverse("customer-addresses", args=[customer.pk])


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


class TestTheEndpoints:
    def test_create_under_the_customer(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            nested(customer),
            {"label": "Head office", "street": "Ataturk Cd.", "is_billing": True},
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["customer_id"] == str(customer.id)
        assert body["is_billing"] is True
        assert body["district_id"] is None

    def test_the_nested_create_refuses_a_customer_in_the_body(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            nested(customer),
            {"customer": str(customer.id), "label": "x", "street": "y"},
            format="json",
        )
        assert response.status_code == 400
        assert [d["field"] for d in response.data["error"]["details"]] == ["customer"]

    def test_list_under_the_customer_is_billing_first(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            CustomerAddress.objects.create(
                company=company, customer=customer, label="B", street="x"
            )
            CustomerAddress.objects.create(
                company=company, customer=customer, label="A", street="x", is_billing=True
            )
        response = api_for(owner).get(nested(customer))
        assert response.status_code == 200
        assert [row["label"] for row in response.json()["results"]] == ["A", "B"]

    def test_the_flat_list_filters_by_customer_and_is_billing(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            other = Customer.objects.create(
                company=company, type=CustomerType.PUBLIC, legal_name="Other"
            )
            CustomerAddress.objects.create(
                company=company, customer=customer, label="Bill", street="x", is_billing=True
            )
            CustomerAddress.objects.create(
                company=company, customer=customer, label="Site", street="x"
            )
            CustomerAddress.objects.create(
                company=company, customer=other, label="Theirs", street="x", is_billing=True
            )
        client = api_for(owner)
        url = reverse("customer-address-list")
        mine = client.get(url, {"customer": str(customer.id)}).json()["results"]
        assert sorted(row["label"] for row in mine) == ["Bill", "Site"]
        billing = client.get(url, {"customer": str(customer.id), "is_billing": "true"}).json()
        assert [row["label"] for row in billing["results"]] == ["Bill"]

    def test_marking_a_second_address_billing_moves_the_flag(self, firm_a):
        company, owner, customer = firm_a
        client = api_for(owner)
        one = client.post(
            nested(customer), {"label": "One", "street": "a", "is_billing": True}, format="json"
        )
        assert one.status_code == 201
        two = client.post(
            nested(customer), {"label": "Two", "street": "b", "is_billing": True}, format="json"
        )
        assert two.status_code == 201
        with company_context(company.id):
            assert CustomerAddress.objects.get(pk=one.json()["id"]).is_billing is False
            assert CustomerAddress.objects.get(pk=two.json()["id"]).is_billing is True

        back = client.patch(
            reverse("customer-address-detail", args=[one.json()["id"]]),
            {"is_billing": True},
            format="json",
        )
        assert back.status_code == 200
        with company_context(company.id):
            assert CustomerAddress.objects.get(pk=two.json()["id"]).is_billing is False
            assert CustomerAddress.objects.get(pk=one.json()["id"]).is_billing is True

    def test_a_billing_address_moved_to_another_customer_demotes_there(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            second = Customer.objects.create(
                company=company, type=CustomerType.PUBLIC, legal_name="Second"
            )
            moving = CustomerAddress.objects.create(
                company=company, customer=customer, label="Moving", street="a", is_billing=True
            )
            resident = CustomerAddress.objects.create(
                company=company, customer=second, label="Resident", street="b", is_billing=True
            )
        response = api_for(owner).patch(
            reverse("customer-address-detail", args=[moving.pk]),
            {"customer": str(second.pk)},
            format="json",
        )
        assert response.status_code == 200
        with company_context(company.id):
            resident.refresh_from_db()
            moving.refresh_from_db()
            assert resident.is_billing is False
            assert moving.is_billing is True
            assert moving.customer_id == second.pk

    def test_delete_is_soft_and_frees_the_billing_slot(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            address = CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="a", is_billing=True
            )
        client = api_for(owner)
        response = client.delete(reverse("customer-address-detail", args=[address.pk]))
        assert response.status_code == 204
        assert CustomerAddress.unscoped.get(pk=address.pk).is_deleted is True
        again = client.post(
            nested(customer), {"label": "Two", "street": "b", "is_billing": True}, format="json"
        )
        assert again.status_code == 201

    def test_another_companys_customer_is_a_404(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, _, customer_b = firm_b
        client = api_for(owner_a)
        assert client.get(nested(customer_b)).status_code == 404
        assert (
            client.post(
                nested(customer_b), {"label": "x", "street": "y"}, format="json"
            ).status_code
            == 404
        )
        flat = client.post(
            reverse("customer-address-list"),
            {"customer": str(customer_b.id), "label": "x", "street": "y"},
            format="json",
        )
        # The same answer the flat contacts endpoint gives: the foreign
        # customer does not exist as far as this company can tell.
        assert flat.status_code == 400
        assert flat.data["error"]["details"] == [{"field": "customer", "code": "does_not_exist"}]


class TestAnAddressMustHoldSomething:
    def test_a_label_alone_is_refused(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(nested(customer), {"label": "Empty"}, format="json")
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "label", "code": "ADDRESS_EMPTY"}]

    @pytest.mark.parametrize("field", ["street", "building_number", "unit_number", "address_note"])
    def test_any_one_content_field_is_enough(self, firm_a, field):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            nested(customer), {"label": "One", field: "something"}, format="json"
        )
        assert response.status_code == 201

    def test_a_neighbourhood_alone_is_enough(self, firm_a):
        from apps.address.models import District, Neighborhood, Province

        _, owner, customer = firm_a
        province = Province.objects.create(id=34, name="P", name_normalized="p")
        district = District.objects.create(id=1, province=province, name="D", name_normalized="d")
        neighborhood = Neighborhood.objects.create(
            id=1, district=district, name="N", name_normalized="n"
        )
        response = api_for(owner).post(
            nested(customer), {"label": "One", "neighborhood": neighborhood.pk}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["district_id"] == district.pk

    def test_a_patch_that_empties_the_last_content_field_is_refused(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            address = CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="a"
            )
        response = api_for(owner).patch(
            reverse("customer-address-detail", args=[address.pk]), {"street": ""}, format="json"
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "label", "code": "ADDRESS_EMPTY"}]

    def test_an_address_with_only_a_unit_number_can_be_relabelled(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            address = CustomerAddress.objects.create(
                company=company, customer=customer, label="One", unit_number="4"
            )
        response = api_for(owner).patch(
            reverse("customer-address-detail", args=[address.pk]), {"label": "New"}, format="json"
        )
        assert response.status_code == 200
