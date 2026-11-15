"""The owner is a flag on a role, and the access token says nothing about roles."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.users import services
from apps.users.models import CompanyRole, Role, User
from core.context import system_context
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.management.commands.create_demo_account import DEFAULT_EMAIL
from tests.test_faults import PASSWORD, Shop


@pytest.fixture
def shop(db) -> Shop:
    return Shop()


def claims(user: User) -> AccessToken:
    return AccessToken(services.issue_tokens(user).access)  # type: ignore[arg-type]


def test_the_access_token_carries_no_role_claim(shop):
    token = claims(shop.owner)

    assert "role" not in token
    assert token["company_id"] == str(shop.company.id)


def test_a_token_with_a_stale_role_claim_is_still_accepted_and_ignored(shop):
    token = AccessToken.for_user(shop.ops)
    token["company_id"] = str(shop.company.id)
    token["role"] = "owner"
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_IDEMPOTENCY_KEY=str(uuid4()))

    assert client.get(reverse("customer-list")).status_code == 200
    # Company settings are the owner's; the claim must not open them.
    changed = client.patch(reverse("company"), {"display_name": "Stale"}, format="json")
    assert changed.status_code == 403


def test_register_company_makes_the_registrant_owner(db):
    company, owner = services.register_company(
        legal_name="New Ltd",
        display_name="New",
        first_name="Ada",
        last_name="Owner",
        email="ada@example.com",
        password=PASSWORD,
    )

    role = owner.loaded_company_role()
    assert role is not None
    assert role.is_owner
    assert role.company_id == company.id
    assert owner.is_owner


def test_last_active_owner_cannot_be_moved_off_the_owner_role(shop):
    with pytest.raises(BusinessRuleError) as raised:
        services.change_role(user=shop.owner, role=Role.ADMIN)

    assert raised.value.get_codes() == ErrorCode.LAST_OWNER_CANNOT_BE_DEACTIVATED.value
    shop.owner.refresh_from_db()
    assert shop.owner.is_owner


def test_a_second_owner_frees_the_first_to_change_role(shop):
    with system_context():
        owner_role = CompanyRole.unscoped.get(company=shop.company, is_owner=True)
        second = User.objects.create_user(
            email="second@example.com",
            password=PASSWORD,
            company=shop.company,
            first_name="Two",
            last_name="Owner",
            company_role=owner_role,
            is_email_verified=True,
        )

    services.change_role(user=shop.owner, role=Role.ADMIN)

    assert not shop.owner.is_owner
    assert second.is_owner


def test_demo_account_seeds_users_on_seeded_roles(db):
    call_command("create_demo_account")

    with system_context():
        user = User.objects.get(email=DEFAULT_EMAIL)
        role = user.loaded_company_role()
        assert role is not None
        assert role.is_owner
        assert role.system_key == Role.OWNER
        assert role.company_id == user.company_id
