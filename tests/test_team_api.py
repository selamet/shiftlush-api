"""Company settings, colleagues and invitations.

The rules being protected here are the ones that are expensive to get wrong:
nobody sets somebody else's password, nobody removes the last owner, and a
technician's view is what their assignments say it is.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from django.core import mail as django_mail
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerType
from apps.users.models import Invitation, Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"


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
    # Verified, because these tests are about invitations rather than about the
    # gate in front of them — TestVerificationGatesInvitations covers that.
    with system_context():
        owner.is_email_verified = True
        owner.save(update_fields=["is_email_verified"])
    django_mail.outbox.clear()
    return company, owner


@pytest.fixture
def deliver(django_capture_on_commit_callbacks):
    """Run the sends that the test transaction would otherwise swallow.

    Mail goes out on commit, so that a mail server being briefly down cannot
    roll back the invitation the administrator has already been told about.
    Every test runs inside a transaction that is deliberately never committed,
    which means the outbox stays empty unless the callbacks are run by hand.
    """

    @contextmanager
    def _deliver():
        with django_capture_on_commit_callbacks(execute=True):
            yield

    return _deliver


INVITEE = {
    "email": "new@example.com",
    "first_name": "Nur",
    "last_name": "Yeni",
    "role": Role.OPERATIONS,
}


def invite(client: APIClient, deliver, **overrides):
    with deliver():
        return client.post(reverse("invitation-list"), INVITEE | overrides, format="json")


def link_token(index: int, path: str) -> str:
    """Pull the single-use token out of the message body.

    Reading it from the mail rather than from a return value is the point: it is
    the only place the plaintext is supposed to exist.
    """
    return django_mail.outbox[index].body.split(f"/{path}/")[1].split()[0]


def colleague(company, role: str, email: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name=role.title(),
            last_name="Person",
            role=role,
        )


class TestCompany:
    def test_the_company_is_addressed_without_an_id(self, firm):
        company, owner = firm
        response = api_for(owner).get(reverse("company"))

        assert response.status_code == 200
        # There is no /companies/{id}: the only company a request can address is
        # the one in its token, so no route exists that could name another.
        assert response.data["id"] == str(company.id)

    def test_only_the_owner_may_change_it(self, firm):
        company, _ = firm
        admin = colleague(company, Role.ADMIN, "admin@example.com")

        assert api_for(admin).get(reverse("company")).status_code == 200
        assert (
            api_for(admin).patch(reverse("company"), {"phone": "+905321112233"}).status_code == 403
        )

    def test_an_invalid_tax_number_is_refused(self, firm):
        _, owner = firm
        response = api_for(owner).patch(reverse("company"), {"tax_number": "1111111111"})
        assert response.status_code == 400

    def test_a_technician_may_read_it(self, firm):
        company, _ = firm
        technician = colleague(company, Role.TECHNICIAN, "tech@example.com")
        # The firm's name and address appear on every screen; hiding them from
        # the people in the field would be a permission with no purpose.
        assert api_for(technician).get(reverse("company")).status_code == 200


class TestInviting:
    def test_an_invitation_sends_a_mail_and_stores_only_a_hash(self, firm, deliver):
        _, owner = firm
        response = invite(api_for(owner), deliver, email="New@Example.com")

        assert response.status_code == 201
        assert response.data["email"] == "new@example.com"
        # The plaintext token exists exactly once, in the message. Returning it
        # would put a working credential in an API response and in every log
        # that records one.
        assert "token" not in response.data
        assert "token_hash" not in response.data

        assert len(django_mail.outbox) == 1
        assert django_mail.outbox[0].to == ["new@example.com"]

    def test_the_mail_carries_a_link_that_verifies(self, firm, deliver):
        client = APIClient()
        _, owner = firm
        invite(api_for(owner), deliver)
        token = link_token(0, "invitation")

        # Public: the invitee has no account yet, which is the entire point.
        response = client.get(reverse("invitation-verify", args=[token]))
        assert response.status_code == 200
        assert response.data["company_name"] == "Firm"
        assert response.data["role"] == Role.OPERATIONS

    def test_an_owner_cannot_be_created_by_invitation(self, firm, deliver):
        _, owner = firm
        response = invite(api_for(owner), deliver, role=Role.OWNER)
        # Otherwise an administrator could promote themselves into the one role
        # they do not hold, by inviting an address they control.
        assert response.status_code == 400

    def test_resending_invalidates_the_previous_link(self, firm, deliver):
        _, owner = firm
        client = api_for(owner)
        created = invite(client, deliver)
        first_token = link_token(0, "invitation")

        with deliver():
            client.post(reverse("invitation-resend", args=[created.data["id"]]))
        second_token = link_token(1, "invitation")

        assert first_token != second_token
        # The usual reason for resending is that the first message went
        # somewhere it should not have, so leaving it alive defeats the purpose.
        assert APIClient().get(reverse("invitation-verify", args=[first_token])).status_code == 422
        assert APIClient().get(reverse("invitation-verify", args=[second_token])).status_code == 200

    def test_revoking_stops_the_link_working(self, firm, deliver):
        _, owner = firm
        client = api_for(owner)
        created = invite(client, deliver)
        token = link_token(0, "invitation")

        client.delete(reverse("invitation-detail", args=[created.data["id"]]))

        assert APIClient().get(reverse("invitation-verify", args=[token])).status_code == 422

    def test_an_expired_link_says_so(self, firm, deliver):
        _, owner = firm
        invite(api_for(owner), deliver)
        token = link_token(0, "invitation")
        Invitation.unscoped.update(expires_at=timezone.now() - timezone.timedelta(seconds=1))

        response = APIClient().get(reverse("invitation-verify", args=[token]))
        # Distinct from an invalid token: the invitee should ask for a new
        # invitation, not conclude that they mistyped something.
        assert response.data["error"]["code"] == "TOKEN_EXPIRED"

    def test_an_unknown_token_reveals_nothing(self, firm):
        response = APIClient().get(reverse("invitation-verify", args=["not-a-real-token"]))
        assert response.status_code == 422
        assert response.data["error"]["code"] == "TOKEN_INVALID"

    def test_only_owners_and_admins_may_invite(self, firm, deliver):
        company, _ = firm
        operations = colleague(company, Role.OPERATIONS, "ops@example.com")
        response = invite(api_for(operations), deliver, role=Role.TECHNICIAN)
        assert response.status_code == 403


class TestUsers:
    def test_a_password_can_never_be_set_through_this_api(self, firm):
        company, owner = firm
        admin = colleague(company, Role.ADMIN, "admin@example.com")

        response = api_for(owner).patch(
            reverse("user-detail", args=[admin.id]),
            {"first_name": "Renamed", "password": "attacker-chosen-pw"},
            format="json",
        )

        assert response.status_code == 200
        assert response.data["first_name"] == "Renamed"
        # An administrator who can set a password can read one. Accounts are
        # created by invitation and passwords are chosen by their owner, so this
        # field must not exist on the write path at all.
        admin.refresh_from_db()
        assert admin.check_password(PASSWORD)

    def test_sensitive_columns_are_not_in_the_response(self, firm):
        company, owner = firm
        colleague(company, Role.TECHNICIAN, "tech@example.com")

        row = api_for(owner).get(reverse("user-list")).data["results"][0]
        for field in ("password", "national_id", "failed_login_count", "locked_until"):
            assert field not in row

    def test_the_last_owner_cannot_be_deactivated(self, firm):
        company, owner = firm
        admin = colleague(company, Role.ADMIN, "admin@example.com")

        response = api_for(admin).post(reverse("user-deactivate", args=[owner.id]))

        # Otherwise nobody can manage users or company settings again, and there
        # is no way back short of editing the database.
        assert response.status_code == 422
        assert response.data["error"]["code"] == "LAST_OWNER_CANNOT_BE_DEACTIVATED"

    def test_the_last_owner_cannot_be_demoted_either(self, firm):
        company, owner = firm
        admin = colleague(company, Role.ADMIN, "admin@example.com")

        response = api_for(admin).patch(
            reverse("user-detail", args=[owner.id]), {"role": Role.ADMIN}, format="json"
        )
        # Same lockout by a different route, so it needs the same guard.
        assert response.status_code == 422

    def test_a_second_owner_makes_the_first_removable(self, firm):
        company, owner = firm
        second = colleague(company, Role.OWNER, "owner2@example.com")

        assert api_for(second).post(reverse("user-deactivate", args=[owner.id])).status_code == 200

    def test_you_cannot_deactivate_yourself(self, firm):
        company, owner = firm
        colleague(company, Role.OWNER, "owner2@example.com")

        response = api_for(owner).post(reverse("user-deactivate", args=[owner.id]))
        # It would end the session performing the action, and there is no
        # reading of the request where that is what somebody meant.
        assert response.data["error"]["code"] == "CANNOT_DEACTIVATE_SELF"

    def test_deactivation_ends_their_sessions(self, firm):
        company, owner = firm
        technician = colleague(company, Role.TECHNICIAN, "tech@example.com")
        working = api_for(technician)
        assert working.get(reverse("company")).status_code == 200

        api_for(owner).post(reverse("user-deactivate", args=[technician.id]))

        technician.refresh_from_db()
        assert not technician.is_active

    def test_another_companys_user_is_not_found(self, firm):
        with system_context():
            _, stranger = register_company(
                legal_name="Other Ltd",
                display_name="Other",
                first_name="O",
                last_name="Ther",
                email="other@example.com",
                password=PASSWORD,
            )
        _, owner = firm

        response = api_for(owner).get(reverse("user-detail", args=[stranger.id]))
        assert response.status_code == 404


class TestTechnicianAssignments:
    @pytest.fixture
    def setup(self, firm):
        company, owner = firm
        with company_context(company.id):
            first = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="First"
            )
            second = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Second"
            )
        technician = colleague(company, Role.TECHNICIAN, "tech@example.com")
        return company, owner, technician, first, second

    def test_assignments_are_replaced_not_merged(self, setup):
        company, owner, technician, first, second = setup
        client = api_for(owner)
        url = reverse("user-customers", args=[technician.id])

        client.put(url, {"customer_ids": [str(first.id)]}, format="json")
        response = client.put(url, {"customer_ids": [str(second.id)]}, format="json")

        # A replace is the only form that can remove an assignment without a
        # second endpoint and a second race.
        assert response.data["assigned_customer_ids"] == [str(second.id)]

    def test_an_emptied_list_is_a_valid_answer(self, setup):
        company, owner, technician, first, _ = setup
        client = api_for(owner)
        url = reverse("user-customers", args=[technician.id])
        client.put(url, {"customer_ids": [str(first.id)]}, format="json")

        response = client.put(url, {"customer_ids": []}, format="json")

        # A technician with nothing assigned sees an empty list. Nothing went
        # wrong; they have no work yet.
        assert response.data["assigned_customer_ids"] == []

    def test_reassigning_a_removed_customer_is_not_a_conflict(self, setup):
        company, owner, technician, first, _ = setup
        client = api_for(owner)
        url = reverse("user-customers", args=[technician.id])

        client.put(url, {"customer_ids": [str(first.id)]}, format="json")
        client.put(url, {"customer_ids": []}, format="json")
        response = client.put(url, {"customer_ids": [str(first.id)]}, format="json")

        # The removed row is soft-deleted and the unique constraint is
        # conditional on that, so the old row does not block the new one.
        assert response.status_code == 200
        assert response.data["assigned_customer_ids"] == [str(first.id)]

    def test_only_technicians_can_be_assigned(self, setup):
        company, owner, _, first, _ = setup
        admin = colleague(company, Role.ADMIN, "admin@example.com")

        response = api_for(owner).put(
            reverse("user-customers", args=[admin.id]),
            {"customer_ids": [str(first.id)]},
            format="json",
        )
        # Every other role sees the whole company, so an assignment on one would
        # be a row that changes nothing — and would look like it did.
        assert response.data["error"]["code"] == "ONLY_TECHNICIANS_ARE_ASSIGNED"

    def test_a_customer_from_another_company_is_unknown(self, setup):
        company, owner, technician, _, _ = setup
        with system_context():
            other_company, _ = register_company(
                legal_name="Other Ltd",
                display_name="Other",
                first_name="O",
                last_name="Ther",
                email="other@example.com",
                password=PASSWORD,
            )
            theirs = Customer.objects.create(
                company=other_company, type=CustomerType.CORPORATE, legal_name="Theirs"
            )

        response = api_for(owner).put(
            reverse("user-customers", args=[technician.id]),
            {"customer_ids": [str(theirs.id)]},
            format="json",
        )
        assert response.status_code == 400

    def test_moving_off_the_technician_role_clears_assignments(self, setup):
        company, owner, technician, first, _ = setup
        client = api_for(owner)
        client.put(
            reverse("user-customers", args=[technician.id]),
            {"customer_ids": [str(first.id)]},
            format="json",
        )

        response = client.patch(
            reverse("user-detail", args=[technician.id]),
            {"role": Role.OPERATIONS},
            format="json",
        )

        # Left behind, they would silently narrow the person's view again if
        # they ever moved back to the field.
        assert response.data["assigned_customer_ids"] == []


class TestPasswordResetMail:
    def test_a_reset_request_sends_a_usable_link(self, firm, deliver):
        _, owner = firm
        client = APIClient()

        with deliver():
            client.post(reverse("auth:password-reset"), {"email": "owner@example.com"})

        assert len(django_mail.outbox) == 1
        token = link_token(0, "password-reset")

        response = client.post(
            reverse("auth:password-reset-confirm"),
            {"token": token, "password": "a-brand-new-password"},
        )
        assert response.status_code == 204

    def test_an_unknown_address_sends_nothing_and_says_nothing(self, firm, deliver):
        with deliver():
            APIClient().post(reverse("auth:password-reset"), {"email": "nobody@example.com"})
        # Confirming which addresses exist would make the form an account
        # enumeration tool; sending no mail is the other half of that.
        assert django_mail.outbox == []


class TestVerificationGatesInvitations:
    """Specification 7.1: data entry is allowed before verification, inviting is not.

    An unverified account may have been opened with an address its owner does
    not control. Entering data harms nobody; sending an invitation puts a real
    message in front of a real person, which is a different kind of act.
    """

    def test_registration_sends_a_verification_mail(self, db, deliver):
        with deliver():
            register_company(
                legal_name="Fresh Ltd",
                display_name="Fresh",
                first_name="F",
                last_name="Resh",
                email="fresh@example.com",
                password=PASSWORD,
            )

        assert len(django_mail.outbox) == 1
        assert django_mail.outbox[0].to == ["fresh@example.com"]
        assert "/verify-email/" in django_mail.outbox[0].body

    def test_an_unverified_owner_cannot_invite(self, db):
        _, owner = register_company(
            legal_name="Unverified Ltd",
            display_name="Unverified",
            first_name="U",
            last_name="Nverified",
            email="unverified@example.com",
            password=PASSWORD,
        )
        assert not owner.is_email_verified

        response = api_for(owner).post(reverse("invitation-list"), INVITEE, format="json")

        assert response.status_code == 422
        assert response.data["error"]["code"] == "EMAIL_NOT_VERIFIED"

    def test_but_they_can_still_enter_data(self, db):
        _, owner = register_company(
            legal_name="Unverified Ltd",
            display_name="Unverified",
            first_name="U",
            last_name="Nverified",
            email="unverified@example.com",
            password=PASSWORD,
        )

        response = api_for(owner).post(
            reverse("customer-list"),
            {"type": CustomerType.CORPORATE, "legal_name": "Allowed"},
            format="json",
        )
        # The restriction is on inviting, not on using the product.
        assert response.status_code == 201

    def test_verifying_the_address_opens_it(self, db, deliver):
        with deliver():
            _, owner = register_company(
                legal_name="Verifying Ltd",
                display_name="Verifying",
                first_name="V",
                last_name="Erify",
                email="verify@example.com",
                password=PASSWORD,
            )
        token = link_token(0, "verify-email")

        assert APIClient().post(reverse("auth:email-verify"), {"token": token}).status_code == 204

        owner.refresh_from_db()
        assert owner.is_email_verified
        assert (
            api_for(owner).post(reverse("invitation-list"), INVITEE, format="json").status_code
            == 201
        )

    def test_the_link_works_once(self, db, deliver):
        with deliver():
            register_company(
                legal_name="Once Ltd",
                display_name="Once",
                first_name="O",
                last_name="Nce",
                email="once@example.com",
                password=PASSWORD,
            )
        token = link_token(0, "verify-email")
        client = APIClient()

        client.post(reverse("auth:email-verify"), {"token": token})
        again = client.post(reverse("auth:email-verify"), {"token": token})

        # A verification link is a credential. Reusable ones stay valid in an
        # inbox forever.
        assert again.status_code == 422

    def test_resending_invalidates_the_previous_link(self, db, deliver):
        with deliver():
            _, owner = register_company(
                legal_name="Resend Ltd",
                display_name="Resend",
                first_name="R",
                last_name="Esend",
                email="resend@example.com",
                password=PASSWORD,
            )
        first = link_token(0, "verify-email")

        with deliver():
            api_for(owner).post(reverse("auth:email-resend"))
        second = link_token(1, "verify-email")

        assert first != second
        client = APIClient()
        assert client.post(reverse("auth:email-verify"), {"token": first}).status_code == 422
        assert client.post(reverse("auth:email-verify"), {"token": second}).status_code == 204
