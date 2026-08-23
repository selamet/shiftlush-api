"""The tenant boundary.

This is the first acceptance criterion in the specification and the one whose
failure is worst: two companies must not be able to reach each other's records
under any circumstance. The layer is only worth having if it is proven, so it is
tested before anything is built on top of it.

Four boundaries live here, from the outside in:

  - **the manager**, which decides whose rows exist at all (TestReadIsolation
    and below);
  - **the HTTP surface**, which is the only place the manager's answer counts —
    parametrised over every detail route the routers publish, so a resource
    added tomorrow is covered on the day it is registered rather than on the day
    somebody remembers (TestEveryRoutedResource);
  - **the assignment boundary inside a company**, which is what a technician
    sees of their own firm (TestTechnicianAssignments);
  - **`system_context()`**, the one place the tenant filter is deliberately off,
    and therefore the place that needs the most proof (TestSystemContextCallers).

Then one test that covers code nobody has written yet: every company-owned model
must default to the tenant manager (TestEveryModelIsScoped). A new model with a
plain ``models.Manager`` passes every other test in this suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from django.apps import apps
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLResolver
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.audit.models import AuditAction, AuditLog
from apps.companies.models import Company
from apps.contracts.models import Contract, ContractElevator, PricingType, Scope
from apps.customers.models import Customer, CustomerContact, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.properties.models import Building, BuildingType, Complex
from apps.users import services
from apps.users.models import Invitation, Role, User, UserCustomer
from core.context import company_context, system_context
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.managers import TenantManager
from core.models import TenantMismatchError
from core.permissions import TechnicianScopedQueryset
from tests.identifiers import tax_number

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "staple-battery-horse"
TODAY = date.today()


@pytest.fixture
def two_companies(db) -> tuple[Company, Company]:
    with system_context():
        first = Company.objects.create(legal_name="First Ltd", display_name="First")
        second = Company.objects.create(legal_name="Second Ltd", display_name="Second")
    return first, second


def make_customer(company: Company, name: str) -> Customer:
    with company_context(company.id):
        return Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name=name
        )


class TestReadIsolation:
    def test_company_sees_only_its_own_rows(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        with company_context(first.id):
            names = list(Customer.objects.values_list("legal_name", flat=True))
        assert names == ["Customer of first"]

    def test_other_companys_row_is_not_reachable_by_id(self, two_companies):
        first, second = two_companies
        theirs = make_customer(second, "Customer of second")

        with company_context(first.id), pytest.raises(Customer.DoesNotExist):
            Customer.objects.get(pk=theirs.pk)

    def test_no_context_returns_nothing_rather_than_everything(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        # A missing context is a bug either way, but it has to fail closed: an
        # unfiltered queryset here would be a cross-tenant read.
        assert Customer.objects.count() == 0

    def test_unscoped_is_the_only_way_across(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        with company_context(first.id):
            assert Customer.unscoped.count() == 2


class TestWriteIsolation:
    def test_saving_under_the_wrong_company_raises(self, two_companies):
        first, second = two_companies
        theirs = make_customer(second, "Customer of second")

        # Filtering reads is only half of it: without the save guard a row could
        # still be attached to another company by setting the FK directly.
        with company_context(first.id), pytest.raises(TenantMismatchError):
            theirs.legal_name = "Renamed by the wrong company"
            theirs.save()

    def test_creating_under_the_wrong_company_raises(self, two_companies):
        first, second = two_companies
        with company_context(first.id), pytest.raises(TenantMismatchError):
            Customer.objects.create(
                company=second, type=CustomerType.CORPORATE, legal_name="Smuggled"
            )


class TestSoftDelete:
    def test_delete_marks_rather_than_removes(self, two_companies):
        first, _ = two_companies
        customer = make_customer(first, "To be deleted")

        with company_context(first.id):
            customer.delete()
            assert Customer.objects.count() == 0
            # The row is still there; only the default manager hides it.
            assert Customer.unscoped.filter(pk=customer.pk, is_deleted=True).exists()

    def test_queryset_delete_also_soft_deletes(self, two_companies):
        first, _ = two_companies
        make_customer(first, "Bulk deleted")

        # QuerySet.delete() bypasses the model's delete(), so without the
        # manager override a bulk delete would destroy rows for real.
        with company_context(first.id):
            Customer.objects.all().delete()
            assert Customer.unscoped.filter(is_deleted=True).count() == 1

    def test_relation_traversal_is_filtered_too(self, two_companies):
        first, _ = two_companies
        customer = make_customer(first, "Parent")

        with company_context(first.id):
            customer.contacts.create(company=first, full_name="Contact")
            assert customer.contacts.count() == 1
            customer.contacts.all().delete()
            # base_manager_name is what makes this hold: without it, following a
            # relation would step around both filters.
            assert customer.contacts.count() == 0


class TestSystemContext:
    def test_registration_can_run_without_a_company(self, db):
        # Company registration and invitation acceptance create the very rows
        # the tenant filter keys on, so they must be able to run with no company
        # in context. Without this escape hatch the save guard would reject the
        # first write a new customer ever makes.
        with system_context():
            company = Company.objects.create(legal_name="Brand New Ltd", display_name="Brand New")
            Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="First customer"
            )

        with company_context(company.id):
            assert Customer.objects.count() == 1


# ---------------------------------------------------------------------------
# The same boundary over HTTP, across every resource the routers publish
# ---------------------------------------------------------------------------


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {services.issue_tokens(user).access}")
    return client


def error_code(response: Any) -> str:
    """The code the client actually switches on.

    Asserted everywhere below instead of the status alone. A 404 from a mistyped
    URL and a 404 from the tenant filter are the same status and different
    events: without this, renaming a route would leave every isolation test in
    this file passing while proving nothing.
    """
    return str(response.data["error"]["code"])


def _named_views() -> dict[str, Any]:
    """Every named URL in the project, with the view that answers it."""

    def walk(resolver: Any) -> Any:
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern)
            elif pattern.name:
                yield pattern.name, pattern.callback

    return dict(walk(get_resolver()))


def _detail_routes() -> dict[str, frozenset[str]]:
    """Router basename -> the verbs its detail route answers.

    Read from the URL conf rather than written out by hand. That is the whole
    point: the next resource somebody registers is covered by the tests below on
    the day it is registered, and a resource that quietly loses its tenant
    filter has nowhere to hide.

    The routed actions are intersected with ``http_method_names`` because the
    router maps PUT onto every viewset with an update method, while
    TenantViewSet refuses PUT — asking for it would prove a 405, not a boundary.
    """
    routes: dict[str, frozenset[str]] = {}
    for name, callback in _named_views().items():
        if not name.endswith("-detail") or not hasattr(callback, "actions"):
            continue
        allowed = set(callback.cls.http_method_names)
        routes[name.removesuffix("-detail")] = frozenset(set(callback.actions) & allowed)
    return routes


DETAIL_ROUTES = _detail_routes()


@dataclass(frozen=True)
class Firm:
    """A company with one row of every resource that has a detail route."""

    company: Company
    owner: User
    records: dict[str, Any]


def _build_firm(letter: str, seed: int) -> Firm:
    company, owner = services.register_company(
        legal_name=f"Firm {letter} Ltd",
        display_name=f"Firm {letter}",
        first_name=letter,
        last_name="Owner",
        email=f"owner-{letter.lower()}@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company,
            type=CustomerType.CORPORATE,
            legal_name=f"Customer of {letter}",
            tax_number=tax_number(seed),
        )
        contact = CustomerContact.objects.create(
            company=company, customer=customer, full_name=f"Contact of {letter}"
        )
        estate = Complex.objects.create(
            company=company, customer=customer, name=f"Complex of {letter}"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name=f"Building of {letter}",
            type=BuildingType.RESIDENTIAL,
            address_note="Behind the market",
        )
        elevator = Elevator(
            company=company,
            building=building,
            registration_number=f"34-2020-00000{seed}",
            name=f"Elevator of {letter}",
        )
        assign_qr_token(elevator)
        elevator.save()
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number=f"{TODAY.year}-000{seed}",
            scope=Scope.MAINTENANCE_AND_REPAIR,
            start_date=TODAY,
            end_date=TODAY + timedelta(days=365),
            pricing_type=PricingType.PER_ELEVATOR,
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=TODAY
        )
        attachment = Attachment.objects.create(
            company=company,
            object_type=ObjectType.ELEVATOR,
            object_id=elevator.id,
            category=AttachmentCategory.PHOTO,
            original_filename=f"{letter}.jpg",
            mime_type="image/jpeg",
            size_bytes=1024,
            storage_key=f"{company.id}/{letter}.jpg",
        )
        invitation = Invitation.objects.create(
            company=company,
            email=f"invited-{letter.lower()}@example.com",
            first_name="Nur",
            last_name=letter,
            role=Role.OPERATIONS,
            token_hash=f"hash-of-{letter}",
            expires_at=timezone.now() + timedelta(hours=72),
            invited_by=owner,
        )
        colleague = User.objects.create_user(
            email=f"tech-{letter.lower()}@example.com",
            password=PASSWORD,
            company=company,
            first_name="Field",
            last_name=letter,
            role=Role.TECHNICIAN,
            is_email_verified=True,
        )

    return Firm(
        company=company,
        owner=owner,
        records={
            "customer": customer,
            "customer-contact": contact,
            "complex": estate,
            "building": building,
            "elevator": elevator,
            "contract": contract,
            "attachment": attachment,
            "invitation": invitation,
            "user": colleague,
        },
    )


@pytest.fixture
def two_firms(db) -> tuple[Firm, Firm]:
    """Two complete firms — one row of every routed resource, twice over."""
    return _build_firm("A", 1), _build_firm("B", 2)


class TestEveryRoutedResource:
    """One firm reaching for another firm's record, on every route there is."""

    def test_the_fixture_covers_every_detail_route(self, two_firms):
        # This is the test that catches the *next* resource. A viewset registered
        # tomorrow appears in DETAIL_ROUTES the moment its router line is
        # written, and this fails until somebody gives it a record to be denied.
        # Fixing it by deleting the assertion is the one wrong answer.
        ours, _ = two_firms
        assert set(ours.records) == set(DETAIL_ROUTES), (
            "A resource is registered on the router with no record in the "
            "cross-tenant fixture, so nothing checks that one firm cannot reach "
            "another firm's copy of it. Add it to _build_firm."
        )

    @pytest.mark.parametrize("basename", sorted(DETAIL_ROUTES))
    def test_another_firms_record_is_not_found(self, basename, two_firms):
        ours, theirs = two_firms
        url = reverse(f"{basename}-detail", args=[theirs.records[basename].pk])
        client = api_for(ours.owner)
        verbs = DETAIL_ROUTES[basename]
        assert verbs, f"{basename}-detail answers no verb, so nothing was tested"

        for verb in sorted(verbs):
            if verb in ("get", "delete"):
                response = getattr(client, verb)(url)
            else:
                response = getattr(client, verb)(url, {}, format="json")

            # 404 rather than 403: a 403 confirms the record exists, which lets
            # an outsider probe for valid ids and count a competitor's estate.
            assert response.status_code == 404, (
                f"{verb.upper()} {basename}-detail answered {response.status_code} "
                f"for another firm's record"
            )
            # And the code, not just the status. A 404 from a route that no
            # longer exists looks identical from the outside; only the body says
            # which of the two happened.
            assert error_code(response) == ErrorCode.NOT_FOUND.value

    @pytest.mark.parametrize("basename", sorted(DETAIL_ROUTES))
    def test_the_other_firms_record_is_untouched(self, basename, two_firms):
        ours, theirs = two_firms
        record = theirs.records[basename]
        url = reverse(f"{basename}-detail", args=[record.pk])
        client = api_for(ours.owner)
        for verb in sorted(DETAIL_ROUTES[basename] - {"get"}):
            if verb == "delete":
                client.delete(url)
            else:
                getattr(client, verb)(url, {}, format="json")

        # A refused write that nevertheless wrote is the failure mode a status
        # code alone would never show.
        with system_context():
            survivor = type(record).unscoped.get(pk=record.pk)
        assert survivor.company_id == theirs.company.id
        assert getattr(survivor, "is_deleted", False) is False

    def test_a_list_never_carries_another_firms_rows(self, two_firms):
        ours, theirs = two_firms
        client = api_for(ours.owner)
        for basename in sorted(DETAIL_ROUTES):
            if "get" not in DETAIL_ROUTES[basename]:
                continue
            try:
                url = reverse(f"{basename}-list")
            except NoReverseMatch:  # pragma: no cover - every router has a list
                continue
            response = client.get(url)
            assert response.status_code == 200, f"{basename}-list: {response.data}"
            rows = response.data["results"]
            theirs_pk = str(theirs.records[basename].pk)
            assert theirs_pk not in {str(row.get("id")) for row in rows}, (
                f"{basename}-list carried another firm's row"
            )


class TestRoutesWithoutADetailView:
    """The four resources the sweep above cannot reach, each on its own terms."""

    def test_a_contract_line_of_another_firm_cannot_be_removed(self, two_firms):
        ours, theirs = two_firms
        response = api_for(ours.owner).delete(
            reverse(
                "contract-remove-elevator",
                args=[theirs.records["contract"].pk, theirs.records["elevator"].pk],
            )
        )
        assert response.status_code == 404
        assert error_code(response) == ErrorCode.NOT_FOUND.value

        with system_context():
            line = ContractElevator.unscoped.get(contract=theirs.records["contract"])
        assert line.removed_at is None

    def test_another_firms_elevator_cannot_be_removed_from_our_own_contract(self, two_firms):
        ours, theirs = two_firms
        response = api_for(ours.owner).delete(
            reverse(
                "contract-remove-elevator",
                args=[ours.records["contract"].pk, theirs.records["elevator"].pk],
            )
        )
        # 422 rather than 404 here, and deliberately so: the contract in the path
        # is ours and does exist, and what is missing is a line on it. The code
        # is still NOT_FOUND, which is what the client acts on.
        assert response.status_code == 422
        assert error_code(response) == ErrorCode.NOT_FOUND.value

    def test_a_qr_token_from_another_firm_resolves_to_nothing(self, two_firms):
        ours, theirs = two_firms
        response = api_for(ours.owner).get(
            reverse("elevator-by-qr", args=[theirs.records["elevator"].qr_token])
        )
        # The sticker is physical and the token is short. A 403 would confirm it
        # is real and let someone map a competitor's estate by trying tokens.
        assert response.status_code == 404
        assert error_code(response) == ErrorCode.NOT_FOUND.value

    def test_the_audit_trail_stops_at_the_company_boundary(self, two_firms):
        ours, theirs = two_firms
        with system_context():
            AuditLog.objects.create(
                company_id=theirs.company.id,
                user_id=theirs.owner.id,
                table_name="customer",
                record_id=theirs.records["customer"].pk,
                action=AuditAction.UPDATE,
            )

        response = api_for(ours.owner).get(reverse("audit-log-list"))
        assert response.status_code == 200
        rows = response.data["results"]
        assert rows, "no entries at all, so nothing was actually filtered"
        assert {row["company_id"] for row in rows} == {str(ours.company.id)}

    def test_the_company_endpoint_answers_only_for_your_own(self, two_firms):
        ours, theirs = two_firms
        response = api_for(ours.owner).get(reverse("company"))
        assert response.status_code == 200
        assert response.data["display_name"] == "Firm A"
        assert response.data["id"] != str(theirs.company.id)

    def test_there_is_no_route_that_takes_a_company_id(self):
        # The endpoint is a singleton on purpose: the only company a request can
        # address is the one already named in its token. A collection would
        # invite a client to try another id, and the only correct answer to that
        # is a 404 — so the route that would produce it does not exist.
        with pytest.raises(NoReverseMatch):
            reverse("company-detail", args=["00000000-0000-7000-8000-000000000000"])

    def test_settings_of_another_firm_cannot_be_written_through_our_own(self, two_firms):
        ours, theirs = two_firms
        api_for(ours.owner).patch(reverse("company"), {"display_name": "Renamed"}, format="json")
        with system_context():
            assert Company.unscoped.get(pk=theirs.company.pk).display_name == "Firm B"


# ---------------------------------------------------------------------------
# The boundary inside a company: what a technician sees of their own firm
# ---------------------------------------------------------------------------


@pytest.fixture
def firm_with_a_technician(db):
    """One firm, two customers, and a technician assigned to exactly one."""
    company, owner = services.register_company(
        legal_name="Assignment Ltd",
        display_name="Assignment",
        first_name="A",
        last_name="Owner",
        email="assignments@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        assigned = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Assigned"
        )
        unassigned = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Unassigned"
        )
        for customer in (assigned, unassigned):
            Building.objects.create(
                company=company,
                customer=customer,
                name=f"Block of {customer.legal_name}",
                type=BuildingType.RESIDENTIAL,
                address_note="",
            )
        technician = User.objects.create_user(
            email="tech@example.com",
            password=PASSWORD,
            company=company,
            first_name="Field",
            last_name="Tech",
            role=Role.TECHNICIAN,
            is_email_verified=True,
        )
        UserCustomer.objects.create(
            company=company, user=technician, customer=assigned, assigned_by=owner
        )
    return company, owner, technician, assigned, unassigned


class TestTechnicianAssignments:
    """The second boundary: the company filter holds, the assignment must too.

    Note that `TechnicianScopedQueryset` is not currently mixed into any viewset
    — the narrowing that runs in production lives in `TenantViewSet.get_queryset`
    — so both are exercised here. A mixin nothing calls is still a mixin the next
    viewset will call.
    """

    def test_the_mixin_narrows_to_the_assigned_customers(self, firm_with_a_technician):
        company, _, technician, assigned, unassigned = firm_with_a_technician
        scoper = TechnicianScopedQueryset()
        scoper.customer_path = "customer"

        with company_context(company.id):
            visible = scoper.scope_to_assignments(Building.objects.all(), technician)
            names = {building.customer.legal_name for building in visible}

        # Remove the .filter() and this reads {"Assigned", "Unassigned"}: the
        # company boundary would still hold and the assignment boundary would
        # be gone, silently.
        assert names == {"Assigned"}

    def test_the_mixin_leaves_every_other_role_alone(self, firm_with_a_technician):
        company, owner, _, _, _ = firm_with_a_technician
        scoper = TechnicianScopedQueryset()
        scoper.customer_path = "customer"

        with company_context(company.id):
            visible = scoper.scope_to_assignments(Building.objects.all(), owner)
            assert visible.count() == 2

    def test_a_technician_with_no_assignments_sees_nothing_rather_than_everything(
        self, firm_with_a_technician
    ):
        company, _, _, _, _ = firm_with_a_technician
        with company_context(company.id):
            spare = User.objects.create_user(
                email="spare@example.com",
                password=PASSWORD,
                company=company,
                first_name="New",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            scoper = TechnicianScopedQueryset()
            scoper.customer_path = "customer"
            assert scoper.scope_to_assignments(Building.objects.all(), spare).count() == 0

    def test_an_unassigned_customer_of_our_own_firm_is_a_404(self, firm_with_a_technician):
        _, _, technician, _, unassigned = firm_with_a_technician
        response = api_for(technician).get(reverse("customer-detail", args=[unassigned.pk]))
        # Same answer as another company's record, and for the same reason: the
        # narrowing changes what exists, not what is permitted.
        assert response.status_code == 404
        assert error_code(response) == ErrorCode.NOT_FOUND.value

    def test_a_list_shows_only_the_assigned_customers(self, firm_with_a_technician):
        _, _, technician, _, _ = firm_with_a_technician
        response = api_for(technician).get(reverse("customer-list"))
        assert [row["legal_name"] for row in response.data["results"]] == ["Assigned"]

    def test_an_unassigned_building_is_out_of_reach_too(self, firm_with_a_technician):
        company, _, technician, _, unassigned = firm_with_a_technician
        with company_context(company.id):
            building = Building.objects.get(customer=unassigned)
        response = api_for(technician).get(reverse("building-detail", args=[building.pk]))
        assert response.status_code == 404
        assert error_code(response) == ErrorCode.NOT_FOUND.value


# ---------------------------------------------------------------------------
# system_context(): the one place the filter is deliberately off
# ---------------------------------------------------------------------------


class TestSystemContextCallers:
    """One test per call site that a foreign identifier can reach.

    `apps/users/services.py` opens `system_context()` fourteen times. The ones
    below are those an outsider can steer with an identifier belonging to
    somebody else — an e-mail address, a refresh token, a session id, a one-time
    token, an invitation link. The remaining call sites take a `User` object the
    server has already resolved, and are exercised through the ones that do.

    Session revocation by id is covered next door, in test_account_security.
    """

    def test_signing_in_scopes_the_session_to_the_signers_own_company(self, two_firms):
        # authenticate() at services.py:147 looks a user up by e-mail with no
        # company bound, because the company comes *from* the user. What must
        # not follow is a session that can see anybody else.
        ours, theirs = two_firms
        client = APIClient()
        response = client.post(
            reverse("auth:login"), {"email": theirs.owner.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.data
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

        listed = client.get(reverse("customer-list")).data["results"]
        assert [row["legal_name"] for row in listed] == ["Customer of B"]

    def test_a_reset_token_changes_only_the_account_it_was_issued_for(self, two_firms):
        # request_password_reset() at :429 and _consume_one_time_token() at :412.
        # Both are reached with an identifier chosen by whoever is asking.
        ours, theirs = two_firms
        with company_context(ours.company.id):
            token = services.request_password_reset(theirs.owner.email)
        assert token is not None

        services.confirm_password_reset(token=token, new_password=NEW_PASSWORD)

        theirs.owner.refresh_from_db()
        ours.owner.refresh_from_db()
        assert theirs.owner.check_password(NEW_PASSWORD)
        assert ours.owner.check_password(PASSWORD), "a reset reached across the boundary"

    def test_a_reset_token_cannot_be_spent_twice(self, two_firms):
        _, theirs = two_firms
        token = services.request_password_reset(theirs.owner.email)
        assert token is not None
        services.confirm_password_reset(token=token, new_password=NEW_PASSWORD)
        with pytest.raises(BusinessRuleError):
            services.confirm_password_reset(token=token, new_password="third-password-here")

    def test_an_unknown_address_yields_no_token_at_all(self, two_firms):
        # The unfiltered lookup must not become an account-enumeration tool. The
        # view answers the same way either way; what is checked here is that the
        # service found nobody rather than somebody it should not have.
        assert services.request_password_reset("nobody@example.com") is None

    def test_an_invitation_lands_the_account_in_the_inviting_company(self, two_firms):
        # invitation_for_token() at :555 and accept_invitation() at :571. These
        # two are the call sites where the system context is load-bearing rather
        # than defensive: Invitation *is* tenant-scoped, so without it the lookup
        # returns nothing and no invitation is ever acceptable.
        ours, theirs = two_firms
        with company_context(theirs.company.id):
            _, token = services.create_invitation(
                company=theirs.company,
                email="joiner@example.com",
                first_name="Yeni",
                last_name="Kisi",
                role=Role.OPERATIONS,
                invited_by=theirs.owner,
            )

        # Accepted while a different company happens to be bound — a warm worker,
        # a background job, a future endpoint that authenticates first. The
        # invitation decides the company; the ambient context never does.
        with company_context(ours.company.id):
            preview = services.invitation_for_token(token)
            joined = services.accept_invitation(token=token, password=PASSWORD)

        assert preview.company_id == theirs.company.id
        assert joined.company_id == theirs.company.id

        listed = api_for(joined).get(reverse("customer-list")).data["results"]
        assert [row["legal_name"] for row in listed] == ["Customer of B"]

    def test_an_accepted_invitation_stops_working(self, two_firms):
        _, theirs = two_firms
        with company_context(theirs.company.id):
            _, token = services.create_invitation(
                company=theirs.company,
                email="joiner2@example.com",
                first_name="Yeni",
                last_name="Kisi",
                role=Role.OPERATIONS,
                invited_by=theirs.owner,
            )
        services.accept_invitation(token=token, password=PASSWORD)
        with pytest.raises(BusinessRuleError):
            services.accept_invitation(token=token, password=PASSWORD)

    def test_a_refresh_token_from_another_company_is_not_our_session(self, two_firms):
        # session_for_refresh_token() at :277. The lookup is scoped to the user
        # as well as the token, so a cookie belonging to somebody else cannot
        # mark one of their chains as ours.
        ours, theirs = two_firms
        theirs_pair = services.issue_tokens(theirs.owner)
        assert services.session_for_refresh_token(ours.owner, theirs_pair.refresh) is None
        assert services.session_for_refresh_token(theirs.owner, theirs_pair.refresh) is not None

    def test_rotating_another_companys_token_leaves_our_sessions_alone(self, two_firms):
        # rotate_refresh_token() at :207, including the replay defence, which
        # revokes every session of the token's own user and nobody else's.
        ours, theirs = two_firms
        ours_pair = services.issue_tokens(ours.owner)
        theirs_pair = services.issue_tokens(theirs.owner)

        services.rotate_refresh_token(refresh_token=theirs_pair.refresh)
        with pytest.raises(BusinessRuleError):
            services.rotate_refresh_token(refresh_token=theirs_pair.refresh)

        # Their replay took out their sessions. Ours is still good.
        assert services.rotate_refresh_token(refresh_token=ours_pair.refresh)

    def test_signing_out_of_another_companys_session_is_not_signing_out_of_ours(self, two_firms):
        # revoke_refresh_token() at :250.
        ours, theirs = two_firms
        ours_pair = services.issue_tokens(ours.owner)
        theirs_pair = services.issue_tokens(theirs.owner)

        services.revoke_refresh_token(theirs_pair.refresh)

        with pytest.raises(BusinessRuleError):
            services.rotate_refresh_token(refresh_token=theirs_pair.refresh)
        assert services.rotate_refresh_token(refresh_token=ours_pair.refresh)

    def test_the_session_list_never_shows_another_companys_devices(self, two_firms):
        # live_sessions() at :296.
        ours, theirs = two_firms
        services.issue_tokens(theirs.owner)
        services.issue_tokens(theirs.owner)
        services.issue_tokens(ours.owner)

        chains = {session.user_id for session in services.live_sessions(ours.owner)}
        assert chains == {ours.owner.id}

    def test_ending_every_other_session_ends_none_of_theirs(self, two_firms):
        # revoke_other_sessions() at :330.
        ours, theirs = two_firms
        services.issue_tokens(ours.owner)
        services.issue_tokens(ours.owner)
        theirs_pair = services.issue_tokens(theirs.owner)

        services.revoke_other_sessions(user=ours.owner, keep=None)

        assert services.live_sessions(ours.owner) == []
        assert services.rotate_refresh_token(refresh_token=theirs_pair.refresh)


# ---------------------------------------------------------------------------
# The test that covers code nobody has written yet
# ---------------------------------------------------------------------------


# Models that carry a company and are deliberately not tenant-managed. Each
# entry is a decision with a reason; adding one is how the decision gets
# reviewed, and the test below refuses entries that no longer name a model.
NOT_TENANT_MANAGED: dict[str, str] = {
    "users.User": (
        "Authentication has to find a user before the company is known, because "
        "the company comes from the user. Scoped by hand in UserViewSet."
    ),
    "audit.AuditLog": (
        "Append-only, and company_id is a plain UUID rather than a foreign key "
        "so an entry outlives its subject. Scoped by hand in AuditLogViewSet."
    ),
    "core.IdempotencyKey": (
        "Infrastructure rather than a business record, hard-deleted when it "
        "expires. Every read already keys on company and user together."
    ),
}


def _carries_a_company(model: Any) -> bool:
    names = {field.name for field in model._meta.get_fields()}
    return bool(names & {"company", "company_id"})


class TestEveryModelIsScoped:
    """Worth more than any individual case: it covers the models not yet written.

    A model declared tomorrow with a plain ``models.Manager`` passes every other
    test in this suite — there is no endpoint yet, so nothing calls it, so
    nothing fails. It fails here, on the day the model is added, which is the
    only day the fix is cheap.
    """

    def test_every_company_owned_model_defaults_to_the_tenant_manager(self):
        offenders = []
        for model in apps.get_models():
            if not _carries_a_company(model) or model._meta.label in NOT_TENANT_MANAGED:
                continue
            if not isinstance(model._default_manager, TenantManager):
                offenders.append(
                    f"{model._meta.label}: default manager is "
                    f"{type(model._default_manager).__name__}, not TenantManager"
                )
        assert offenders == [], (
            "A model owned by a company is readable without the company filter. "
            "Derive it from CompanyOwnedModel, or add it to NOT_TENANT_MANAGED "
            "with the reason: " + "; ".join(offenders)
        )

    def test_every_company_owned_model_is_scoped_through_its_relations_too(self):
        # base_manager is what a traversal uses — building.elevator_set and
        # every forward FK fetch. A model whose default manager is scoped and
        # whose base manager is not leaks the moment somebody follows a relation
        # instead of querying the model.
        offenders = []
        for model in apps.get_models():
            if not _carries_a_company(model) or model._meta.label in NOT_TENANT_MANAGED:
                continue
            if not isinstance(model._meta.base_manager, TenantManager):
                offenders.append(
                    f"{model._meta.label}: base manager is "
                    f"{type(model._meta.base_manager).__name__}, not TenantManager"
                )
        assert offenders == [], (
            "base_manager_name has been lost, so following a relation steps "
            "around the tenant boundary: " + "; ".join(offenders)
        )

    def test_the_exemption_list_names_only_real_models(self):
        # Otherwise the list rots into a place to hide things: a model renamed
        # or removed leaves a stale entry that silently exempts nothing, and the
        # next model to take that label inherits the exemption.
        carrying = {model._meta.label for model in apps.get_models() if _carries_a_company(model)}
        assert set(NOT_TENANT_MANAGED) <= carrying, (
            "NOT_TENANT_MANAGED names something that is not a model carrying a "
            f"company: {sorted(set(NOT_TENANT_MANAGED) - carrying)}"
        )

    def test_the_models_this_suite_knows_about_are_all_of_them(self):
        # A guard on the guard. If someone adds a company-owned model and gives
        # it a TenantManager, the tests above pass and nothing tells anyone the
        # HTTP sweep has not been extended. This names the set out loud, so
        # adding a model is a two-line diff that has to be looked at.
        expected = {
            "attachments.Attachment",
            "audit.AuditLog",
            "contracts.Contract",
            "contracts.ContractElevator",
            "core.IdempotencyKey",
            "customers.Customer",
            "customers.CustomerContact",
            "elevators.Elevator",
            "properties.Building",
            "properties.Complex",
            "users.Invitation",
            "users.User",
            "users.UserCustomer",
        }
        actual = {model._meta.label for model in apps.get_models() if _carries_a_company(model)}
        assert actual == expected, (
            "The set of models that belong to a company has changed. Give the "
            "new one a cross-tenant test — over HTTP if it is routed — before "
            "updating this list."
        )

    def test_the_unscoped_manager_is_the_only_way_across(self):
        # Named to be conspicuous, and it has to stay the only escape hatch:
        # a second unfiltered manager on a company-owned model would be one
        # nobody reviews.
        for model in apps.get_models():
            if not isinstance(model._default_manager, TenantManager):
                continue
            unfiltered = {
                name
                for name, manager in model._meta.managers_map.items()
                if not isinstance(manager, TenantManager)
            }
            assert unfiltered == {"unscoped"}, (
                f"{model._meta.label} has an unfiltered manager other than "
                f"`unscoped`: {sorted(unfiltered - {'unscoped'})}"
            )
