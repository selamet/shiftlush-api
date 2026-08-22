"""Authentication behaviour, end to end through the API."""

from __future__ import annotations

from typing import ClassVar

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

    @pytest.mark.parametrize(
        ("password", "accepted", "why"),
        [
            ("t7wq3z", True, "six characters, the floor"),
            ("t7wq3", False, "five, one under"),
            ("123456", False, "six, but on the common-password blocklist"),
            ("parola", False, "six, but blocklisted"),
            ("evebay", False, "six, but derived from the address"),
        ],
    )
    def test_password_floor_is_six(self, client, db, password, accepted, why):
        """Pin the floor exactly, and the two rules that carry it.

        The old version of this test used a five-character password, which
        fails against a floor of ten and a floor of six alike — so it passed
        without ever proving where the line was. These cases fail if the floor
        moves in either direction.

        The last three matter more than the first: at six characters the
        blocklist and the similarity check are most of the protection, and a
        password reaching the floor is not the same as a password worth having.
        """
        response = client.post(
            reverse("auth:register"),
            {
                "legal_name": "Short Ltd",
                "display_name": "Short",
                "first_name": "Eve",
                "last_name": "Bay",
                "email": "evebay@example.com",
                "password": password,
            },
        )
        assert response.status_code == (201 if accepted else 400), why

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

    def test_ready_checks_the_database_and_storage(self, client, db, monkeypatch):
        from core import views

        # Storage is not running in the test environment; the point here is that
        # both checks are reported, not that MinIO is up.
        monkeypatch.setattr(views.storage, "reachable", lambda backend: True)

        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {"database": "ok", "storage": "ok"}

    def test_ready_fails_when_storage_is_unreachable(self, client, db, monkeypatch):
        from core import views

        monkeypatch.setattr(views.storage, "reachable", lambda backend: False)

        response = client.get("/ready")
        # An instance that cannot reach the bucket hands out upload URLs that
        # fail at the confirmation step, which the user reads as their file
        # disappearing. Better to take it out of rotation.
        assert response.status_code == 503
        assert response.json()["checks"]["storage"] == "unreachable"


class TestSessionPayload:
    def test_me_carries_the_company_name(self, client, owner):
        login(client)
        token = client.post(reverse("auth:login"), {"email": EMAIL, "password": PASSWORD}).data[
            "access"
        ]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        body = client.get(reverse("auth:me")).data

        # The shell shows the firm's name on every screen. Making that a second
        # request puts an independently failing call on the boot path for one
        # string, and the topbar renders empty while it is in flight.
        assert body["company_name"] == "Test Elevator"

    def test_the_name_costs_no_extra_query(self, client, owner, django_assert_num_queries):
        token = client.post(reverse("auth:login"), {"email": EMAIL, "password": PASSWORD}).data[
            "access"
        ]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # One query: the user, joined to the company. Without the join this is
        # two, and the same second query appears on invitation creation and on
        # label printing, which both read request.user.company.
        with django_assert_num_queries(1):
            client.get(reverse("auth:me"))

    def test_login_returns_it_too(self, client, owner):
        body = login(client).data
        # Same serializer, so the client has the name before its first
        # authenticated request rather than after it.
        assert body["user"]["company_name"] == "Test Elevator"


class TestReadinessNeverRaises:
    """A probe that throws is worse than one that says no.

    An unreachable bucket is the condition /ready exists to report. When the
    check raised instead, the endpoint returned a 500 HTML page and whatever
    reads it — a load balancer, a deploy script — got no usable answer at all.
    Found by running the built image against a bucket that does not exist.
    """

    def test_an_unreachable_bucket_reports_not_ready(self, client, db, settings):
        settings.STORAGE_BACKENDS = {
            **settings.STORAGE_BACKENDS,
            "local": {
                "endpoint_url": "https://nothing.invalid",
                "access_key_id": "x",
                "secret_access_key": "x",
                "bucket": "x",
                "region": "us-east-1",
            },
        }

        response = client.get("/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["storage"] == "unreachable"

    def test_a_backend_that_is_not_configured_reports_not_ready(self, client, db, settings):
        settings.DEFAULT_STORAGE_BACKEND = "nonexistent"

        response = client.get("/ready")
        # Not a crash either: a deployment pointing at a backend it has no
        # credentials for should be reported, not raised.
        assert response.status_code == 503
        assert response.json()["checks"]["storage"] == "unreachable"


class TestInfrastructureEndpointsAreReachableWithoutTls:
    """Docker's healthcheck and a load balancer talk to the container directly.

    Both speak plain HTTP on the loopback inside the host, and neither sets
    X-Forwarded-Proto. With the SSL redirect applying to them they got a 301 to
    an https port that does not exist, so the container reported itself
    unhealthy for its whole life while serving traffic correctly through Caddy.
    """

    @pytest.mark.parametrize("path", ["/health", "/ready"])
    def test_no_redirect_on_a_plain_http_request(self, client, db, settings, path):
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_REDIRECT_EXEMPT = [r"^health$", r"^ready$"]

        response = client.get(path)

        # Any 3xx here is the bug: the caller has nowhere to follow it to.
        assert response.status_code in (200, 503), response.status_code

    def test_a_normal_endpoint_still_redirects(self, client, db, settings):
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_REDIRECT_EXEMPT = [r"^health$", r"^ready$"]

        # The exemption is two paths, not a hole in the policy.
        assert client.get("/api/v1/customers").status_code == 301


class TestMailMustBeEncrypted:
    """A misconfigured EMAIL_URL leaks the SMTP password, it does not just fail.

    django-environ takes TLS from the scheme and ignores `?tls=True`, which is
    the form most documentation suggests. Without TLS, SMTP AUTH sends the
    provider's API key across the internet in clear text. Resend happens to
    refuse the unencrypted session, so it surfaced at the first send; a provider
    that accepted it would have leaked the credential on every message.

    Loads the real production settings module, so the test fails if the guard is
    removed rather than asserting a copy of it.
    """

    REQUIRED: ClassVar[dict[str, str]] = {
        "DJANGO_SECRET_KEY": "x",
        "DJANGO_ALLOWED_HOSTS": "example.com",
        "DATABASE_URL": "postgres://u:p@localhost/db",
        "CORS_ALLOWED_ORIGINS": "https://example.com",
        "CSRF_TRUSTED_ORIGINS": "https://example.com",
        "FIELD_ENCRYPTION_KEY": "x",
        "FRONTEND_URL": "https://example.com",
        "DEFAULT_FROM_EMAIL": "a@example.com",
        "R2_ENDPOINT_URL": "https://example.com",
        "R2_ACCESS_KEY_ID": "x",
        "R2_SECRET_ACCESS_KEY": "x",
        "R2_BUCKET_NAME": "x",
    }

    def _load(self, monkeypatch, email_url: str):
        import importlib

        for key, value in self.REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("EMAIL_URL", email_url)
        return importlib.reload(importlib.import_module("config.settings.production"))

    @pytest.mark.parametrize(
        "url",
        [
            "smtp://resend:key@smtp.example.com:587",
            # The form most documentation suggests, and the one that looks
            # obviously right. It is silently ignored.
            "smtp://resend:key@smtp.example.com:587/?tls=True",
        ],
    )
    def test_it_refuses_to_boot(self, monkeypatch, url):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="TLS"):
            self._load(monkeypatch, url)

    @pytest.mark.parametrize(
        "url",
        [
            "smtp+tls://resend:key@smtp.example.com:587",
            "smtp+ssl://resend:key@smtp.example.com:465",
        ],
    )
    def test_the_documented_schemes_boot(self, monkeypatch, url):
        settings_module = self._load(monkeypatch, url)
        assert settings_module.EMAIL_USE_TLS or settings_module.EMAIL_USE_SSL
