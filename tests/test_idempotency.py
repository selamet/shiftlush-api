"""Replay protection.

The scenario: a bad connection, a tap on save, nothing visibly happens, another
tap. Without this there are now two contracts, both looking legitimate.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core import mail as django_mail
from django.core.management import call_command
from django.urls import get_resolver, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contracts.models import Contract, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.users.models import Invitation, Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context
from core.models import IdempotencyKey
from tests.identifiers import tax_number

PASSWORD = "correct-horse-battery"
TODAY = date.today()
KEY = "9f1c0e2a-retry-once"


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
            company=company, type=CustomerType.CORPORATE, legal_name="A customer"
        )
    return company, owner, customer


def payload(customer_id) -> dict:
    return {
        "customer": str(customer_id),
        "scope": Scope.MAINTENANCE_ONLY,
        "start_date": str(TODAY),
        "end_date": str(TODAY + timedelta(days=365)),
        "pricing_type": PricingType.FLAT,
        "monthly_fee": "1000.00",
        "vat_rate": "20.00",
    }


class TestReplay:
    def test_the_same_key_creates_one_record(self, firm):
        company, owner, customer = firm
        client = api_for(owner)

        first = client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        second = client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        assert first.status_code == second.status_code == 201
        # The second call returns the first answer rather than making a second
        # contract, so the client cannot tell the retry happened.
        assert first.data["id"] == second.data["id"]
        with company_context(company.id):
            assert Contract.objects.count() == 1

    def test_a_different_key_creates_a_second_record(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        client.post(
            reverse("contract-list"),
            payload(customer.id),
            format="json",
            HTTP_IDEMPOTENCY_KEY="a-different-key",
        )
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_no_key_means_no_protection(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(reverse("contract-list"), payload(customer.id), format="json")
        client.post(reverse("contract-list"), payload(customer.id), format="json")
        # The header is optional; a client that does not send one gets the
        # behaviour it had before.
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_the_same_key_with_a_different_body_is_refused(self, firm):
        _, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        changed = payload(customer.id) | {"monthly_fee": "9999.00"}
        response = client.post(
            reverse("contract-list"), changed, format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        # Serving the stored answer here would reply to a question that was
        # never asked — one of the hardest bugs to trace afterwards.
        assert response.status_code == 409
        assert response.data["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_an_expired_key_no_longer_replays(self, firm):
        company, owner, customer = firm
        client = api_for(owner)
        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        IdempotencyKey.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        client.post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        with company_context(company.id):
            assert Contract.objects.count() == 2

    def test_a_failed_request_is_not_stored(self, firm):
        _, owner, customer = firm
        client = api_for(owner)
        broken = payload(customer.id) | {"end_date": str(TODAY - timedelta(days=1))}

        assert (
            client.post(
                reverse("contract-list"), broken, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            ).status_code
            == 400
        )
        # Storing a failure would make a transient error permanent for a day.
        assert not IdempotencyKey.objects.filter(key=KEY).exists()

        assert (
            client.post(
                reverse("contract-list"),
                payload(customer.id),
                format="json",
                HTTP_IDEMPOTENCY_KEY=KEY,
            ).status_code
            == 201
        )

    def test_keys_do_not_cross_between_users(self, firm):
        from core.context import system_context

        company, owner, customer = firm
        with system_context():
            other = User.objects.create_user(
                email="other@example.com",
                password=PASSWORD,
                company=company,
                first_name="O",
                last_name="Ther",
                role=owner.role,
            )

        api_for(owner).post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        api_for(other).post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )
        # Two people can pick the same key by coincidence; one must not receive
        # the other's response.
        with company_context(company.id):
            assert Contract.objects.count() == 2


class TestExpiredKeysAreSweptUp:
    """Issue #60: the request path only ever drops a key that comes back.

    An expired row is deleted lazily, when the same `(company, user, key)` is
    presented a second time — which is exactly the case that needed no cleaning
    up. A key presented once is never read again, and that is nearly all of
    them: the header is insurance against a retry that usually never comes. So
    the table keeps every write the deployment has ever served, and nothing says
    so until someone looks at the disk. Section 5.15 asks for a daily command.
    """

    def stored(self, firm) -> None:
        _, owner, customer = firm
        api_for(owner).post(
            reverse("contract-list"), payload(customer.id), format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

    def test_a_key_past_its_window_is_deleted(self, firm):
        self.stored(firm)
        IdempotencyKey.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        call_command("purge_idempotency_keys")

        # Hard deleted, not marked: there is nothing here worth keeping once the
        # window closes, and no soft-delete column to keep it in.
        assert not IdempotencyKey.objects.exists()

    def test_a_key_still_inside_its_window_is_left_alone(self, firm):
        self.stored(firm)

        call_command("purge_idempotency_keys")

        # A sweep that also took live keys would turn the retry it exists for
        # into a duplicate record.
        assert IdempotencyKey.objects.count() == 1

    def test_a_dry_run_counts_and_deletes_nothing(self, firm):
        self.stored(firm)
        IdempotencyKey.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        output = StringIO()
        call_command("purge_idempotency_keys", "--dry-run", stdout=output)

        assert "would purge 1 key(s)" in output.getvalue()
        assert IdempotencyKey.objects.count() == 1

    def test_it_reports_what_it_removed(self, firm):
        self.stored(firm)
        IdempotencyKey.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        output = StringIO()
        call_command("purge_idempotency_keys", stdout=output)

        # Whoever reads the cron log needs a number, not silence: silence looks
        # the same whether the command worked or never ran.
        assert "purged 1 key(s)" in output.getvalue()


class TestEveryCreateIsProtected:
    """Protection is a property of the base viewset, not a decorator to remember.

    It used to be opt-in on two endpoints. Adding a resource meant remembering
    the decorator, forgetting it was invisible, and the symptom only ever showed
    up on a bad connection in front of a user — who then had two records that
    both looked legitimate.

    Found against the deployed server: the client sends the header on every
    create, and customers silently ignored it.
    """

    def test_a_second_customer_is_not_created(self, firm):
        company, owner, _ = firm
        client = api_for(owner)
        body = {
            "type": CustomerType.CORPORATE,
            "legal_name": "Only once",
            "tax_number": tax_number(1),
        }

        first = client.post(reverse("customer-list"), body, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
        second = client.post(
            reverse("customer-list"), body, format="json", HTTP_IDEMPOTENCY_KEY=KEY
        )

        assert first.status_code == second.status_code == 201
        assert first.data["id"] == second.data["id"]
        with company_context(company.id):
            assert Customer.objects.filter(legal_name="Only once").count() == 1

    def test_a_second_building_is_not_created(self, firm):
        from apps.properties.models import Building, BuildingType

        company, owner, customer = firm
        client = api_for(owner)
        body = {
            "customer": str(customer.id),
            "name": "A Blok",
            "type": BuildingType.RESIDENTIAL,
            "address_note": "Test",
        }

        client.post(reverse("building-list"), body, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
        client.post(reverse("building-list"), body, format="json", HTTP_IDEMPOTENCY_KEY=KEY)

        with company_context(company.id):
            assert Building.objects.filter(name="A Blok").count() == 1

    def test_the_same_key_with_a_different_body_is_still_refused(self, firm):
        _, owner, _ = firm
        client = api_for(owner)
        client.post(
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "First", "tax_number": tax_number(2)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=KEY,
        )

        response = client.post(
            reverse("customer-list"),
            {
                "type": CustomerType.CORPORATE,
                "legal_name": "Different",
                "tax_number": tax_number(3),
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=KEY,
        )
        assert response.status_code == 409

    def test_a_client_that_sends_no_key_is_unaffected(self, firm):
        from apps.properties.models import Building, BuildingType

        company, owner, customer = firm
        client = api_for(owner)
        # A building rather than a customer: a customer now carries a unique tax
        # number, so two identical ones are refused by that rule and the header
        # never gets a chance to be the reason. The claim under test is about
        # the header, so it needs a resource where the duplicate is legal.
        body = {
            "customer": str(customer.id),
            "name": "No key",
            "type": BuildingType.RESIDENTIAL,
            "address_note": "Test",
        }

        client.post(reverse("building-list"), body, format="json")
        client.post(reverse("building-list"), body, format="json")

        # The header is optional and stays optional.
        with company_context(company.id):
            assert Building.objects.filter(name="No key").count() == 2


INVITEE = {
    "email": "new@example.com",
    "first_name": "Nur",
    "last_name": "Yeni",
    "role": Role.OPERATIONS,
}


@pytest.fixture
def verified(firm, django_capture_on_commit_callbacks):
    """An owner who may invite, and a way to see the mail they send.

    Invitations go out on commit, so that a mail server being briefly down
    cannot roll back the record the administrator has already been told about.
    Inside a test transaction that never commits, the outbox stays empty unless
    the callbacks are run by hand.
    """
    company, owner, _ = firm
    with system_context():
        owner.is_email_verified = True
        owner.save(update_fields=["is_email_verified"])
    django_mail.outbox.clear()

    @contextmanager
    def deliver():
        with django_capture_on_commit_callbacks(execute=True):
            yield

    return company, owner, deliver


class TestInvitationsAreProtectedToo:
    """The hole the base viewset did not cover — issue #44.

    `InvitationViewSet` is not a `TenantViewSet`: there is no update, and its
    create mints a token and sends a message rather than saving a row. It
    inherited none of the protection, and accepted `Idempotency-Key` the way any
    endpoint accepts any header — by ignoring it.

    A duplicate invitation is worse than a duplicate row. Each one mails its own
    live token and nothing invalidates the first, so the invitee ends up holding
    two working links for one seat.
    """

    def test_a_retry_creates_one_invitation_and_sends_one_mail(self, verified):
        company, owner, deliver = verified
        client = api_for(owner)

        with deliver():
            first = client.post(
                reverse("invitation-list"), INVITEE, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            )
        with deliver():
            second = client.post(
                reverse("invitation-list"), INVITEE, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            )

        assert first.status_code == second.status_code == 201
        # The stored answer rather than a second one: an administrator who
        # pressed send twice on a weak connection sees the invitation they
        # already sent.
        assert first.data["id"] == second.data["id"]
        with company_context(company.id):
            assert Invitation.objects.filter(email=INVITEE["email"]).count() == 1
        assert len(django_mail.outbox) == 1

    def test_the_replay_is_the_original_answer_and_not_a_fresh_one(self, verified):
        _, owner, deliver = verified
        client = api_for(owner)

        with deliver():
            first = client.post(
                reverse("invitation-list"), INVITEE, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            )
        with deliver():
            second = client.post(
                reverse("invitation-list"), INVITEE, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            )

        # Every field, not only the id, and compared as the client receives it:
        # the stored body is JSON, so this is the comparison that says the two
        # callers got the same answer. A replay has to answer the question that
        # was asked the first time, or the retry is a different call wearing the
        # same status code.
        assert second.json() == first.json()

    def test_the_same_key_with_a_different_body_is_refused(self, verified):
        company, owner, deliver = verified
        client = api_for(owner)
        with deliver():
            client.post(
                reverse("invitation-list"), INVITEE, format="json", HTTP_IDEMPOTENCY_KEY=KEY
            )

        with deliver():
            response = client.post(
                reverse("invitation-list"),
                INVITEE | {"email": "someone.else@example.com"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=KEY,
            )

        # Serving the stored answer here would tell the administrator that
        # somebody they never invited has been invited.
        assert response.status_code == 409
        assert response.data["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        with company_context(company.id):
            assert not Invitation.objects.filter(email="someone.else@example.com").exists()
        assert len(django_mail.outbox) == 1

    def test_a_client_that_sends_no_key_still_gets_one_invitation(self, verified):
        company, owner, deliver = verified
        client = api_for(owner)

        with deliver():
            client.post(reverse("invitation-list"), INVITEE, format="json")
        with deliver():
            second = client.post(reverse("invitation-list"), INVITEE, format="json")

        # The header stays optional, so this address is defended by the rule
        # behind it instead: one live invitation per address, and a resend for
        # the case where the first message went astray.
        assert second.status_code == 422
        assert second.data["error"]["code"] == "INVITATION_ALREADY_PENDING"
        with company_context(company.id):
            assert Invitation.objects.filter(email=INVITEE["email"]).count() == 1


def create_routes() -> list[tuple[str, type]]:
    """Every route the router maps POST to a `create` on, read from the URLconf."""

    def walk(patterns, prefix=""):
        for entry in patterns:
            nested = getattr(entry, "url_patterns", None)
            if nested is not None:
                yield from walk(nested, prefix + str(entry.pattern))
                continue
            actions = getattr(entry.callback, "actions", None) or {}
            if actions.get("post") == "create":
                yield prefix + str(entry.pattern), entry.callback.cls

    return list(walk(get_resolver().url_patterns))


class TestNoCreateEndpointCanForgetThis:
    """Asked of the router rather than of a list kept by hand.

    Protection was opt-in once, and moved onto the base viewset the day a
    resource was found without it. That covered everything inheriting from the
    base and nothing else — `InvitationViewSet` was the next endpoint written
    the same way, and nothing failed when it appeared.

    Inheriting the mixin is what makes an endpoint protected; this is what makes
    forgetting to inherit it visible. A viewset that writes its own `create` and
    inherits nothing fails here, in testing, rather than in front of a user on a
    bad connection.
    """

    def test_the_walk_actually_finds_create_endpoints(self):
        # A walker that quietly found nothing would make every assertion below
        # pass while checking nothing at all.
        assert len(create_routes()) >= 5

    @pytest.mark.parametrize(("route", "view"), create_routes())
    def test_every_create_is_protected_or_says_why_not(self, route, view):
        create = view.create
        protected = getattr(create, "replay_protected", False)
        exempt = getattr(create, "replay_exempt", False)

        assert protected or exempt, (
            f"POST {route} ({view.__name__}) creates a record and ignores "
            f"Idempotency-Key. Inherit core.idempotency.ReplayProtectedCreate, or "
            f"mark the method @replay_exempt if a retry genuinely cannot duplicate."
        )

    def test_the_exemptions_are_the_ones_that_were_argued_for(self):
        exempt = {
            view.__name__
            for _, view in create_routes()
            if getattr(getattr(view, "create", None), "replay_exempt", False)
        }
        # Confirming an upload converges by itself: a storage key names one
        # object, so a retry returns the record the first call made. Any other
        # name arriving here is a decision to be made rather than inherited.
        assert exempt == {"AttachmentViewSet"}
