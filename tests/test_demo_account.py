"""The demonstration account, and the promise that it can be made again.

The point of `create_demo_account` is that the account survives a database being
rebuilt, so the tests that matter are the second run, the password policy, and
the blast radius of `--with-data` — not that one command writes one row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.companies.models import Company
from apps.customers.models import Customer, CustomerType
from apps.users.models import Role, User
from core import demo
from core.context import company_context, system_context
from core.management.commands.create_demo_account import DEFAULT_EMAIL, DEFAULT_PASSWORD
from tests.identifiers import tax_number

#: Three provinces, enough for buildings to have somewhere to be. The full
#: dataset is the subject of tests/test_address_bootstrap.py and costs seconds.
SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")


@pytest.fixture
def addresses(db) -> None:
    call_command("load_address_data", path=SAMPLE_DATA)


def demo_user() -> User | None:
    with system_context():
        return User.objects.filter(email=DEFAULT_EMAIL).first()


class TestTheAccountItCreates:
    def test_creates_one_company_and_its_owner(self, db):
        call_command("create_demo_account")

        with system_context():
            assert Company.objects.count() == 1
        user = demo_user()
        assert user is not None
        assert user.email == DEFAULT_EMAIL
        assert user.check_password(DEFAULT_PASSWORD)

    def test_the_account_is_an_owner(self, db):
        """Anything less hides the company-settings screens from the demo.

        `company`/WRITE is owner-only in the permission matrix, so an `admin`
        demo would open the product with a page it cannot use.
        """
        call_command("create_demo_account")

        user = demo_user()
        assert user is not None
        assert user.role == Role.OWNER

    def test_the_address_is_already_verified(self, db):
        """Provisioned, not claimed.

        Nobody has to prove they control an address somebody else chose for
        them, and leaving the flag off would leave a hand-edit in the admin as a
        step somebody has to remember on every new environment.
        """
        call_command("create_demo_account")

        user = demo_user()
        assert user is not None
        assert user.is_email_verified

    def test_it_cannot_deactivate_itself(self, db):
        """It is the only owner of its company, so the rule that protects every

        other firm protects this one: a company that loses its last owner has
        nobody who can manage users or company settings again.
        """
        from apps.users.services import deactivate_user
        from core.error_codes import ErrorCode
        from core.exceptions import BusinessRuleError

        call_command("create_demo_account")
        user = demo_user()
        assert user is not None

        with pytest.raises(BusinessRuleError) as refused:
            deactivate_user(user=user)

        detail = refused.value.detail
        assert not isinstance(detail, list | dict)
        assert detail.code == ErrorCode.LAST_OWNER_CANNOT_BE_DEACTIVATED.value

    def test_it_can_sign_in(self, db):
        """The whole deliverable, asserted through the API rather than the ORM.

        Sign-in does not consult `is_email_verified`; this is what proves it,
        and it is the question somebody asks every time a demo is handed over.
        """
        call_command("create_demo_account")

        response = APIClient().post(
            reverse("auth:login"), {"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}
        )

        assert response.status_code == 200
        assert response.data["user"]["email"] == DEFAULT_EMAIL
        assert response.data["user"]["role"] == Role.OWNER


class TestRunningItAgain:
    def test_a_second_run_creates_no_second_company(self, db):
        call_command("create_demo_account")
        call_command("create_demo_account")

        with system_context():
            assert Company.objects.count() == 1
            assert User.objects.filter(email=DEFAULT_EMAIL).count() == 1

    def test_a_second_run_leaves_a_changed_password_alone(self, db):
        """Restoring it would silently undo a deliberate change.

        The command defines who the demo account is, not what its password is
        this week.
        """
        call_command("create_demo_account")
        user = demo_user()
        assert user is not None
        user.set_password("something-else-entirely")
        user.save(update_fields=["password"])

        call_command("create_demo_account")

        user.refresh_from_db()
        assert user.check_password("something-else-entirely")


class TestThePasswordPolicy:
    def test_the_chosen_password_meets_it(self, db):
        """The credential the product owner picked, checked against the real

        validators rather than against a reading of them. `demo` appears in
        `demo@selamet.dev`, so the similarity check is the one at risk; if a
        future validator refuses this password, it fails here rather than on a
        production box at midnight.
        """
        call_command("create_demo_account")

        user = demo_user()
        assert user is not None
        assert user.check_password(DEFAULT_PASSWORD)

    def test_a_short_password_is_refused(self, db):
        with pytest.raises(CommandError, match="AUTH_PASSWORD_VALIDATORS"):
            call_command("create_demo_account", password="abc")

    def test_a_common_password_is_refused(self, db):
        with pytest.raises(CommandError, match="AUTH_PASSWORD_VALIDATORS"):
            call_command("create_demo_account", password="password")

    def test_a_password_too_like_the_address_is_refused(self, db):
        with pytest.raises(CommandError, match="AUTH_PASSWORD_VALIDATORS"):
            call_command("create_demo_account", email="demo@example.com", password="demo@exampl")

    def test_a_refused_password_writes_nothing(self, db):
        with pytest.raises(CommandError):
            call_command("create_demo_account", password="abc")

        with system_context():
            assert not Company.objects.exists()
            assert not User.objects.exists()

    def test_an_address_belonging_to_nobody_is_refused(self, db):
        """A superuser has no company, so it can never become the demo owner."""
        User.objects.create_superuser(
            email="root@example.com", password=DEFAULT_PASSWORD, first_name="Root", last_name="User"
        )

        with pytest.raises(CommandError, match="belongs to no company"):
            call_command("create_demo_account", email="root@example.com")


class TestWhereTheGeneratedDataLands:
    def test_without_the_flag_nothing_is_generated(self, addresses):
        call_command("create_demo_account")

        company = Company.objects.get()
        with company_context(company.id):
            assert not Customer.objects.exists()

    def test_with_the_flag_the_demo_company_is_filled(self, addresses):
        call_command("create_demo_account", "--with-data")

        company = Company.objects.get()
        with company_context(company.id):
            assert Customer.objects.count() == len(demo.CUSTOMERS)

    def test_another_company_is_left_alone(self, addresses):
        """The reason this may be pointed at production at all.

        The tenant boundary is what makes it true, so the assertion is on the
        other company's rows rather than on the names of the generated ones.
        """
        theirs = Company.objects.create(legal_name="Real Elevator Ltd", display_name="Real")
        with company_context(theirs.id):
            Customer.objects.create(
                company=theirs,
                type=CustomerType.CORPORATE,
                legal_name="Their One Customer",
                tax_number=tax_number(1),
            )

        call_command("create_demo_account", "--with-data")

        with company_context(theirs.id):
            assert Customer.objects.count() == 1
            assert Customer.objects.get().legal_name == "Their One Customer"

    def test_running_it_twice_does_not_double_the_data(self, addresses):
        call_command("create_demo_account", "--with-data")
        company = Company.objects.get()
        with company_context(company.id):
            before = Customer.objects.count()

        call_command("create_demo_account", "--with-data")

        with company_context(company.id):
            assert Customer.objects.count() == before

    def test_a_tenant_that_already_holds_records_is_not_filled(self, addresses):
        """`--email` pointed at a real account is the danger this closes."""
        theirs = Company.objects.create(legal_name="Real Elevator Ltd", display_name="Real")
        User.objects.create_user(
            email="real@example.com",
            password=DEFAULT_PASSWORD,
            company=theirs,
            first_name="Real",
            last_name="Owner",
            role=Role.OWNER,
        )
        User.objects.create_user(
            email="colleague@example.com",
            password=DEFAULT_PASSWORD,
            company=theirs,
            first_name="Real",
            last_name="Colleague",
            role=Role.OPERATIONS,
        )

        call_command("create_demo_account", "--with-data", email="real@example.com")

        with company_context(theirs.id):
            assert not Customer.objects.exists()

    def test_the_data_needs_somewhere_to_be(self, db):
        with pytest.raises(CommandError, match="load_address_data"):
            call_command("create_demo_account", "--with-data")


class TestItCoexistsWithSeedDemoData:
    """Both commands in one database.

    `User.email` is unique across the whole table, so two demonstration tenants
    would collide on the first technician if they shared a staff domain. This is
    what the `staff_domain` argument on `core.demo.populate` is for, and CI runs
    exactly this pair against a clean database.
    """

    def test_both_can_run_against_one_database(self, addresses):
        call_command("seed_demo_data")
        call_command("create_demo_account", "--with-data")

        with system_context():
            assert Company.objects.count() == 2
        seeded = Company.objects.exclude(display_name="ShiftLush Demo").get()
        for company in (seeded, Company.objects.get(display_name="ShiftLush Demo")):
            with company_context(company.id):
                assert Customer.objects.count() == len(demo.CUSTOMERS)

    def test_seed_demo_data_still_refuses_a_populated_database(self, addresses):
        call_command("create_demo_account")

        with pytest.raises(CommandError, match="already holds a company"):
            call_command("seed_demo_data")
