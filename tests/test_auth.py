"""Authentication behaviour, end to end through the API."""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users.models import RefreshSession, Role, User
from apps.users.services import MAX_FAILED_LOGINS, register_company
from core.context import system_context

PASSWORD = "correct-horse-battery"
EMAIL = "owner@example.com"

REFRESH_COOKIE = "shiftlush_refresh"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(db) -> User:
    _, user = register_company(
        legal_name="Test Elevator Ltd",
        display_name="Test Elevator",
        first_name="Test",
        last_name="Owner",
        email=EMAIL,
        password=PASSWORD,
    )
    return user


def login(client: APIClient, email: str = EMAIL, password: str = PASSWORD):
    return client.post(reverse("auth:login"), {"email": email, "password": password})


class TestRegistration:
    def test_creates_a_company_and_its_owner(self, client, db):
        response = client.post(
            reverse("auth:register"),
            {
                "legal_name": "New Elevator Ltd",
                "display_name": "New Elevator",
                "first_name": "Ada",
                "last_name": "Owner",
                "email": "ada@example.com",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201
        assert response.data["user"]["role"] == Role.OWNER
        # Registration runs with no company in context; without the system
        # escape hatch the tenant guard would reject the very first write.
        with system_context():
            assert User.objects.get(email="ada@example.com").company is not None

    def test_duplicate_email_is_named(self, client, owner):
        response = client.post(
            reverse("auth:register"),
            {
                "legal_name": "Another Ltd",
                "display_name": "Another",
                "first_name": "Bob",
                "last_name": "Owner",
                "email": EMAIL,
                "password": PASSWORD,
            },
        )
        # Not a vague validation failure: the user needs to know it is the
        # address, since one address belongs to one company by design.
        assert response.status_code == 422
        assert response.data["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    def test_short_password_is_rejected(self, client, db):
        response = client.post(
            reverse("auth:register"),
            {
                "legal_name": "Short Ltd",
                "display_name": "Short",
                "first_name": "Eve",
                "last_name": "Owner",
                "email": "eve@example.com",
                "password": "short",
            },
        )
        assert response.status_code == 400

    def test_unknown_field_is_rejected(self, client, db):
        # DRF would ignore it, and the value would silently never arrive.
        response = client.post(
            reverse("auth:register"),
            {
                "legal_name": "Typo Ltd",
                "display_name": "Typo",
                "first_name": "Eve",
                "last_name": "Owner",
                "emial": "typo@example.com",
                "email": "typo@example.com",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 400


class TestLogin:
    def test_returns_access_in_body_and_refresh_in_a_cookie(self, client, owner):
        response = login(client)
        assert response.status_code == 200
        assert response.data["access"]

        cookie = response.cookies[REFRESH_COOKIE]
        # The refresh token must be unreachable from JavaScript: an injected
        # script that can read it owns the account for thirty days.
        assert cookie["httponly"]
        assert cookie["samesite"] == "Lax"
        # And it must never appear in the body, where a script could read it.
        assert "refresh" not in response.data

    def test_wrong_password_and_unknown_address_look_identical(self, client, owner):
        wrong = login(client, password="wrong-password-entirely")
        unknown = login(client, email="nobody@example.com")
        assert wrong.status_code == unknown.status_code == 422
        # Any difference here would turn the login form into a way to find out
        # who has an account.
        assert wrong.data["error"]["code"] == unknown.data["error"]["code"]

    def test_lockout_after_repeated_failures(self, client, owner):
        for _ in range(MAX_FAILED_LOGINS):
            login(client, password="wrong-password-entirely")

        response = login(client)  # correct password, still locked
        assert response.data["error"]["code"] == "ACCOUNT_LOCKED"

    def test_success_clears_the_failure_counter(self, client, owner):
        login(client, password="wrong-password-entirely")
        login(client)
        with system_context():
            assert User.objects.get(pk=owner.pk).failed_login_count == 0

    def test_inactive_account_cannot_sign_in(self, client, owner):
        with system_context():
            User.objects.filter(pk=owner.pk).update(is_active=False)
        assert login(client).data["error"]["code"] == "ACCOUNT_INACTIVE"


class TestRefreshRotation:
    def test_refresh_returns_a_new_pair(self, client, owner):
        login(client)
        response = client.post(reverse("auth:refresh"))
        assert response.status_code == 200
        assert response.data["access"]

    def test_the_old_token_stops_working(self, client, owner):
        login(client)
        first = client.cookies[REFRESH_COOKIE].value
        client.post(reverse("auth:refresh"))

        client.cookies[REFRESH_COOKIE] = first
        assert client.post(reverse("auth:refresh")).status_code == 422

    def test_replaying_a_used_token_kills_every_session(self, client, owner):
        login(client)
        stolen = client.cookies[REFRESH_COOKIE].value
        client.post(reverse("auth:refresh"))  # legitimate holder rotates

        # A revoked token coming back means a copy is in circulation. There is
        # no way to tell the victim from the attacker, so both are signed out:
        # logging the victim out is cheap, leaving the attacker in is not.
        client.cookies[REFRESH_COOKIE] = stolen
        assert client.post(reverse("auth:refresh")).status_code == 422

        with system_context():
            assert not RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).exists()

    def test_expired_token_is_rejected(self, client, owner):
        login(client)
        with system_context():
            RefreshSession.objects.filter(user=owner).update(
                expires_at=timezone.now() - timezone.timedelta(seconds=1)
            )
        assert client.post(reverse("auth:refresh")).data["error"]["code"] == "TOKEN_EXPIRED"

    def test_missing_cookie_is_rejected(self, client, db):
        assert client.post(reverse("auth:refresh")).status_code == 422


class TestLogout:
    def test_revokes_only_this_session(self, client, owner):
        login(client)
        other = APIClient()
        login(other)

        client.post(reverse("auth:logout"))

        # Signing out of one browser must not sign the user out of their phone.
        assert other.post(reverse("auth:refresh")).status_code == 200


class TestPasswordReset:
    def test_response_is_the_same_for_unknown_addresses(self, client, owner):
        known = client.post(reverse("auth:password-reset"), {"email": EMAIL})
        unknown = client.post(reverse("auth:password-reset"), {"email": "nobody@example.com"})
        assert known.status_code == unknown.status_code == 202

    def test_reset_signs_out_every_session(self, client, owner):
        from apps.users.services import request_password_reset

        login(client)
        token = request_password_reset(EMAIL)
        assert token is not None

        response = client.post(
            reverse("auth:password-reset-confirm"),
            {"token": token, "password": "brand-new-password"},
        )
        assert response.status_code == 204

        # If the reset happened because the account was taken over, leaving the
        # attacker's sessions alive would defeat the point.
        with system_context():
            assert not RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).exists()

    def test_a_token_works_once(self, client, owner):
        from apps.users.services import request_password_reset

        token = request_password_reset(EMAIL)
        client.post(
            reverse("auth:password-reset-confirm"), {"token": token, "password": "first-attempt-x"}
        )
        second = client.post(
            reverse("auth:password-reset-confirm"),
            {"token": token, "password": "second-attempt-x"},
        )
        assert second.data["error"]["code"] == "TOKEN_INVALID"

    def test_issuing_a_new_token_invalidates_the_previous_one(self, client, owner):
        from apps.users.services import request_password_reset

        first = request_password_reset(EMAIL)
        request_password_reset(EMAIL)

        # Otherwise a forwarded old e-mail stays usable after a fresh request.
        response = client.post(
            reverse("auth:password-reset-confirm"), {"token": first, "password": "using-the-old-x"}
        )
        assert response.data["error"]["code"] == "TOKEN_INVALID"


class TestMe:
    def test_requires_authentication(self, client, db):
        assert client.get(reverse("auth:me")).status_code == 401

    def test_returns_the_signed_in_user(self, client, owner):
        access = login(client).data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = client.get(reverse("auth:me"))
        assert response.status_code == 200
        assert response.data["email"] == EMAIL

    def test_never_exposes_sensitive_columns(self, client, owner):
        access = login(client).data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        body = client.get(reverse("auth:me")).data
        # These all live on User and none of them belong in a response. Listing
        # fields explicitly is what keeps a new sensitive column off the API.
        for field in ("password", "national_id", "failed_login_count", "locked_until"):
            assert field not in body


class TestInfrastructureEndpoints:
    def test_health_is_unversioned_and_open(self, client, db):
        assert client.get("/health").status_code == 200

    def test_ready_checks_the_database(self, client, db):
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["checks"]["database"] == "ok"
