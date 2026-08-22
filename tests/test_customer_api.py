"""The customer endpoints, and the two boundaries they have to hold.

The unit tests proved the manager filters and the matrix answers correctly.
This proves the same things through HTTP, which is the only place it counts —
a boundary that holds in a unit test and leaks through a viewset is no boundary.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerContact, CustomerType
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
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "New", "tax_number": "1234567808"},
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
            {
                "type": CustomerType.CORPORATE,
                "legal_name": "Phone",
                "tax_number": "1234567808",
                "phone": "0555 123 45 67",
            },
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


# Valid check digits, computed once so a reader does not have to wonder whether
# a failure is the rule under test or a rejected number.
VKN = "1234567808"
OTHER_VKN = "9876543217"
TCKN = "10000000146"
OTHER_TCKN = "23456789060"


def corporate(**overrides) -> dict:
    body = {"type": CustomerType.CORPORATE, "legal_name": "Acme A.Ş.", "tax_number": VKN}
    return {**body, **overrides}


def individual(**overrides) -> dict:
    body = {"type": CustomerType.INDIVIDUAL, "legal_name": "Ayşe Yılmaz", "national_id": TCKN}
    return {**body, **overrides}


class TestPrimaryContact:
    """Only one contact per customer is primary, and saying so is not an error.

    The constraint that enforces it lives in the database, so before this it
    surfaced as a 500 — a rule the user cannot see, reported as a crash.
    """

    def test_marking_a_second_contact_primary_moves_the_flag(self, firm_a):
        company, owner, customer = firm_a
        client = api_for(owner)
        url = reverse("customer-contact-list")
        first = client.post(
            url, {"customer": str(customer.pk), "full_name": "Önceki", "is_primary": True}
        )
        assert first.status_code == 201

        second = client.post(
            url, {"customer": str(customer.pk), "full_name": "Yeni", "is_primary": True}
        )
        # The user marked someone primary; that is an instruction, not a clash.
        assert second.status_code == 201
        assert second.data["is_primary"] is True

        with company_context(company.id):
            primaries = list(
                CustomerContact.objects.filter(customer=customer, is_primary=True).values_list(
                    "full_name", flat=True
                )
            )
        assert primaries == ["Yeni"]

    def test_promoting_an_existing_contact_demotes_the_other(self, firm_a):
        company, owner, customer = firm_a
        client = api_for(owner)
        url = reverse("customer-contact-list")
        first = client.post(
            url, {"customer": str(customer.pk), "full_name": "Önceki", "is_primary": True}
        )
        second = client.post(url, {"customer": str(customer.pk), "full_name": "Yeni"})

        promoted = client.patch(
            reverse("customer-contact-detail", args=[second.data["id"]]), {"is_primary": True}
        )
        assert promoted.status_code == 200

        with company_context(company.id):
            assert not CustomerContact.objects.get(pk=first.data["id"]).is_primary

    def test_another_customers_primary_is_left_alone(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            other = Customer.objects.create(
                company=company, type=CustomerType.PUBLIC, legal_name="Other"
            )
        client = api_for(owner)
        url = reverse("customer-contact-list")
        theirs = client.post(
            url, {"customer": str(other.pk), "full_name": "Theirs", "is_primary": True}
        )
        client.post(url, {"customer": str(customer.pk), "full_name": "Ours", "is_primary": True})

        with company_context(company.id):
            assert CustomerContact.objects.get(pk=theirs.data["id"]).is_primary


class TestContactsUnderTheCustomer:
    """`/customers/{id}/contacts` — specification §8.6."""

    def test_the_list_holds_that_customers_contacts_only(self, firm_a):
        company, owner, customer = firm_a
        with company_context(company.id):
            other = Customer.objects.create(
                company=company, type=CustomerType.PUBLIC, legal_name="Other"
            )
            CustomerContact.objects.create(company=company, customer=customer, full_name="Ours")
            CustomerContact.objects.create(company=company, customer=other, full_name="Theirs")

        response = api_for(owner).get(reverse("customer-contacts", args=[customer.pk]))
        assert response.status_code == 200
        assert [row["full_name"] for row in response.data["results"]] == ["Ours"]

    def test_another_firms_customer_is_a_404(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, _, customer_b = firm_b
        response = api_for(owner_a).get(reverse("customer-contacts", args=[customer_b.pk]))
        assert response.status_code == 404

    def test_posting_attaches_the_contact_to_the_customer_in_the_path(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            reverse("customer-contacts", args=[customer.pk]),
            {"full_name": "Hasan Kaya", "role": "manager", "phone": "0555 123 45 67"},
        )
        assert response.status_code == 201
        assert str(response.data["customer_id"]) == str(customer.pk)
        assert response.data["phone"] == "+905551234567"

    def test_naming_the_customer_in_the_body_is_refused(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            reverse("customer-contacts", args=[customer.pk]),
            {"customer": str(customer.pk), "full_name": "Hasan"},
        )
        # Two sources for one value is how they disagree. The path is the source.
        assert response.status_code == 400

    def test_posting_to_another_firms_customer_is_a_404(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, _, customer_b = firm_b
        response = api_for(owner_a).post(
            reverse("customer-contacts", args=[customer_b.pk]), {"full_name": "Smuggled"}
        )
        assert response.status_code == 404

    def test_a_replayed_submission_creates_one_contact(self, firm_a):
        _, owner, customer = firm_a
        client = api_for(owner)
        url = reverse("customer-contacts", args=[customer.pk])
        first = client.post(url, {"full_name": "Hasan"}, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        second = client.post(url, {"full_name": "Hasan"}, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        assert first.status_code == second.status_code == 201
        assert first.data["id"] == second.data["id"]

    def test_a_technician_cannot_post(self, firm_a):
        company, _, customer = firm_a
        user = make_user(company, Role.TECHNICIAN, "tech-contacts@example.com")
        with company_context(company.id):
            user.customer_assignments.create(company=company, customer=customer)
        response = api_for(user).post(
            reverse("customer-contacts", args=[customer.pk]), {"full_name": "Nope"}
        )
        assert response.status_code == 403


class TestSearch:
    """Turkish folding — specification §9.2.

    `İ`.lower() is two code points, so the standard lowercase never matches what
    is stored. Searching for a customer by name is the first thing anyone does,
    and before this it silently returned nothing.
    """

    @pytest.fixture
    def searchable(self, firm_a):
        company, owner, _ = firm_a
        with company_context(company.id):
            Customer.objects.create(
                company=company,
                type=CustomerType.COMPLEX_MANAGEMENT,
                legal_name="Şişli Site Yönetimi",
            )
        return api_for(owner)

    @pytest.mark.parametrize("term", ["sisli", "şişli", "ŞİŞLİ", "Şişli", "SISLI", "site"])
    def test_the_name_is_found_however_it_is_typed(self, searchable, term):
        response = searchable.get(reverse("customer-list"), {"search": term})
        assert [row["legal_name"] for row in response.data["results"]] == ["Şişli Site Yönetimi"]

    def test_an_unrelated_term_matches_nothing(self, searchable):
        response = searchable.get(reverse("customer-list"), {"search": "beşiktaş"})
        assert response.data["results"] == []

    def test_a_renamed_customer_is_found_under_its_new_name(self, firm_a):
        _, owner, customer = firm_a
        client = api_for(owner)
        client.patch(
            reverse("customer-detail", args=[customer.pk]), {"legal_name": "Üsküdar Yönetim"}
        )
        response = client.get(reverse("customer-list"), {"search": "uskudar"})
        # The normalised copy is only useful if it cannot drift from the name.
        assert [row["legal_name"] for row in response.data["results"]] == ["Üsküdar Yönetim"]


class TestDuplicateIdentifiers:
    def test_a_second_customer_with_the_same_tax_number_is_refused(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        assert client.post(reverse("customer-list"), corporate()).status_code == 201
        second = client.post(reverse("customer-list"), corporate(legal_name="Acme Two"))
        assert second.status_code == 409
        assert second.data["error"]["code"] == "DUPLICATE_TAX_NUMBER"

    def test_a_second_individual_with_the_same_national_id_is_refused(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        assert client.post(reverse("customer-list"), individual()).status_code == 201
        second = client.post(reverse("customer-list"), individual(legal_name="Ayşe Y."))
        # The column is encrypted with a fresh nonce per write, so this can only
        # be caught through the fingerprint, never by comparing ciphertext.
        assert second.status_code == 409
        assert second.data["error"]["code"] == "DUPLICATE_NATIONAL_ID"

    def test_the_national_id_is_not_stored_in_the_clear(self, firm_a):
        from django.db import connection

        _, owner, _ = firm_a
        created = api_for(owner).post(reverse("customer-list"), individual())
        # Straight to the column. Reading it through the ORM decrypts on the way
        # out, so it would pass whatever is on disk.
        # The key has to be adapted the way the engine stores it — SQLite keeps
        # a UUID as undashed hex, Postgres as a uuid.
        key = Customer._meta.pk.get_db_prep_value(created.data["id"], connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT national_id, national_id_fingerprint FROM customer WHERE id = %s", [key]
            )
            stored, digest = cursor.fetchone()
        assert TCKN not in stored
        assert TCKN not in digest
        assert digest != ""

    def test_another_company_may_hold_the_same_tax_number(self, firm_a, firm_b):
        _, owner_a, _ = firm_a
        _, owner_b, _ = firm_b
        assert api_for(owner_a).post(reverse("customer-list"), corporate()).status_code == 201
        # Two firms serving the same building management company is ordinary.
        assert api_for(owner_b).post(reverse("customer-list"), corporate()).status_code == 201

    def test_a_deleted_customer_releases_its_tax_number(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(reverse("customer-list"), corporate())
        client.delete(reverse("customer-detail", args=[created.data["id"]]))
        again = client.post(reverse("customer-list"), corporate(legal_name="Acme Again"))
        # Otherwise a number is held forever by a record nobody can see.
        assert again.status_code == 201

    def test_updating_a_customer_onto_a_taken_tax_number_is_refused(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        client.post(reverse("customer-list"), corporate())
        other = client.post(
            reverse("customer-list"), corporate(legal_name="B", tax_number=OTHER_VKN)
        )
        moved = client.patch(
            reverse("customer-detail", args=[other.data["id"]]), {"tax_number": VKN}
        )
        assert moved.status_code == 409

    def test_a_customer_keeps_its_own_tax_number_on_update(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(reverse("customer-list"), corporate())
        response = client.patch(
            reverse("customer-detail", args=[created.data["id"]]),
            {"legal_name": "Acme Renamed", "tax_number": VKN},
        )
        # Comparing against itself is how a save-without-changes turns into an error.
        assert response.status_code == 200


class TestTypeRules:
    """Which identifier belongs to which kind of customer.

    An individual with a tax office is not a validation nicety — it is a record
    that will be invoiced under the wrong identity.
    """

    def test_an_individual_may_not_carry_a_tax_number(self, firm_a):
        _, owner, _ = firm_a
        response = api_for(owner).post(reverse("customer-list"), individual(tax_number=VKN))
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "tax_number", "code": "FIELD_NOT_VALID_FOR_CUSTOMER_TYPE"}
        ]

    def test_an_individual_may_not_carry_a_tax_office(self, firm_a):
        _, owner, _ = firm_a
        response = api_for(owner).post(reverse("customer-list"), individual(tax_office="Kadıköy"))
        assert response.status_code == 400

    def test_an_individual_needs_a_national_id(self, firm_a):
        _, owner, _ = firm_a
        body = individual()
        del body["national_id"]
        response = api_for(owner).post(reverse("customer-list"), body)
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "national_id", "code": "FIELD_REQUIRED_FOR_CUSTOMER_TYPE"}
        ]

    def test_an_organisation_may_not_carry_a_national_id(self, firm_a):
        _, owner, _ = firm_a
        response = api_for(owner).post(reverse("customer-list"), corporate(national_id=TCKN))
        assert response.status_code == 400

    def test_an_organisation_needs_a_tax_number(self, firm_a):
        _, owner, _ = firm_a
        body = corporate()
        del body["tax_number"]
        response = api_for(owner).post(reverse("customer-list"), body)
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "customer_type",
        [
            CustomerType.COMPLEX_MANAGEMENT,
            CustomerType.BUILDING_MANAGEMENT,
            CustomerType.CORPORATE,
            CustomerType.PUBLIC,
        ],
    )
    def test_every_organisation_type_is_held_to_the_same_rule(self, firm_a, customer_type):
        _, owner, _ = firm_a
        response = api_for(owner).post(
            reverse("customer-list"),
            {"type": customer_type, "legal_name": "No number", "tax_number": ""},
        )
        assert response.status_code == 400

    def test_a_well_formed_individual_is_accepted(self, firm_a):
        _, owner, _ = firm_a
        response = api_for(owner).post(reverse("customer-list"), individual())
        assert response.status_code == 201
        assert "national_id" not in response.data

    def test_changing_type_is_held_to_the_rule_of_the_new_type(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(reverse("customer-list"), corporate())
        response = client.patch(
            reverse("customer-detail", args=[created.data["id"]]),
            {"type": CustomerType.INDIVIDUAL},
        )
        # The tax number it already holds is now the wrong identifier for it.
        assert response.status_code == 400

    def test_a_partial_update_is_judged_against_the_stored_type(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(reverse("customer-list"), individual())
        response = client.patch(
            reverse("customer-detail", args=[created.data["id"]]), {"tax_number": VKN}
        )
        # `type` is not in the payload; the rule still has to know what it is.
        assert response.status_code == 400

    def test_clearing_the_required_identifier_is_refused(self, firm_a):
        _, owner, _ = firm_a
        client = api_for(owner)
        created = client.post(reverse("customer-list"), corporate())
        response = client.patch(
            reverse("customer-detail", args=[created.data["id"]]), {"tax_number": ""}
        )
        assert response.status_code == 400

    def test_an_unrelated_field_can_still_be_edited_on_a_record_that_predates_the_rule(
        self, firm_a
    ):
        # `firm_a`'s customer was created straight through the ORM without a tax
        # number, which is exactly the shape of every row already in the table.
        _, owner, customer = firm_a
        response = api_for(owner).patch(
            reverse("customer-detail", args=[customer.pk]), {"notes": "Called on Tuesday"}
        )
        # Refusing this would freeze those records: every edit would fail,
        # including the one that would have completed them.
        assert response.status_code == 200
