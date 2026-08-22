"""The request limits, and what a client is told about them.

Specification 8.13 sets two: twenty a minute per address without a token, three
hundred a minute per user with one. 8.9 says every response reports the quota,
and a refusal says when to come back.

The assertions here are about *which* request is refused, not merely that one
eventually is. A limit that bites on the tenth request instead of the twenty
first passes "a 429 happened" and is a different product — half the honest
clients in a busy office would be broken by it, and nobody would find out from
a test that only counted refusals.

The limits are switched on one test at a time by patching the rate table on the
throttle class. That is not indirection for its own sake: DRF binds the table to
the class when the module is imported, so `settings.REST_FRAMEWORK` cannot reach
it afterwards — the same reason `tests/test_geocode.py` does it this way. Why
the suite runs with them off at all is explained in `config/settings/test.py`.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.users.services import register_company
from core.client_ip import client_ip
from core.throttling import DefaultRateThrottle

PASSWORD = "correct-horse-battery"

#: What the specification asks for, and what `config/settings/base.py`
#: configures. Written out rather than imported from the settings module so that
#: a change to the numbers has to be made here too, in front of a reviewer.
ANON_LIMIT = 20
USER_LIMIT = 300


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    # The counter lives in the cache and the locmem cache outlives a test, so
    # without this every test after the first would start part-way through
    # somebody else's window.
    cache.clear()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Switch the default limits on, at the configured rates or smaller ones."""

    def apply(anon: str | None = None, user: str | None = None) -> None:
        monkeypatch.setattr(
            DefaultRateThrottle,
            "THROTTLE_RATES",
            {
                "anon": anon or f"{ANON_LIMIT}/min",
                "user": user or f"{USER_LIMIT}/min",
                "geocode": "60/min",
            },
        )

    return apply


def make_user(email: str = "user@example.com") -> None:
    register_company(
        legal_name="Test Ltd",
        display_name="Test",
        first_name="Test",
        last_name="User",
        email=email,
        password=PASSWORD,
    )


def authenticated(email: str = "user@example.com", **extra: str) -> APIClient:
    api = APIClient(**extra)
    access = api.post(reverse("auth:login"), {"email": email, "password": PASSWORD}).data["access"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}", **extra)
    return api


def anonymous_call(api: APIClient) -> Any:
    """One request that any caller may make, with nothing to authenticate.

    Refresh with no cookie: it is refused at the first line of the view, so it
    costs no query and nothing accumulates in the database over three hundred of
    them. What is being counted is the request, not what it did.
    """
    return api.post(reverse("auth:refresh"))


class TestTheAnonymousLimit:
    def test_the_twentieth_request_is_answered_and_the_twenty_first_is_not(self, db, limits):
        limits()
        api = APIClient()

        answered = [anonymous_call(api).status_code for _ in range(ANON_LIMIT)]
        refused = anonymous_call(api)

        # Every one of the twenty reached the view — 422 is the view refusing a
        # refresh with no cookie, which is the endpoint working.
        assert answered == [422] * ANON_LIMIT
        assert refused.status_code == 429
        assert refused.data["error"]["code"] == "THROTTLED"

    def test_the_refusal_says_how_long_to_wait(self, db, limits):
        limits()
        api = APIClient()
        for _ in range(ANON_LIMIT):
            anonymous_call(api)

        refused = anonymous_call(api)

        # A minute window that has only just filled: the earliest slot frees
        # about sixty seconds from now, and never more than that.
        assert 0 < int(refused["Retry-After"]) <= 60
        assert refused["RateLimit-Limit"] == "20"
        assert refused["RateLimit-Remaining"] == "0"
        # At zero remaining the two answer the same question and must agree,
        # or a client that trusts one waits differently from one that trusts
        # the other.
        assert refused["RateLimit-Reset"] == refused["Retry-After"]

    def test_each_answer_counts_down(self, db, limits):
        limits()
        api = APIClient()

        first = anonymous_call(api)
        second = anonymous_call(api)

        assert first["RateLimit-Limit"] == "20"
        assert first["RateLimit-Remaining"] == "19"
        assert second["RateLimit-Remaining"] == "18"
        assert 0 < int(first["RateLimit-Reset"]) <= 60

    def test_two_addresses_are_counted_separately(self, db, limits):
        limits()
        exhausted = APIClient(REMOTE_ADDR="203.0.113.10")
        for _ in range(ANON_LIMIT):
            anonymous_call(exhausted)

        assert anonymous_call(exhausted).status_code == 429
        # A neighbour on a different address is unaffected — the limit is per
        # caller, not a global cap on the endpoint.
        assert anonymous_call(APIClient(REMOTE_ADDR="203.0.113.11")).status_code == 422


class TestTheAuthenticatedLimit:
    def test_the_three_hundredth_request_is_answered_and_the_next_is_not(self, db, limits):
        limits()
        make_user()
        api = authenticated()
        me = reverse("auth:me")

        statuses = {api.get(me).status_code for _ in range(USER_LIMIT)}
        refused = api.get(me)

        # The login that fetched the token was anonymous and went to the other
        # bucket, so all three hundred of these are the user's own allowance.
        assert statuses == {200}
        assert refused.status_code == 429
        assert refused.data["error"]["code"] == "THROTTLED"

    def test_a_token_buys_the_larger_allowance_not_the_smaller_one(self, db, limits):
        limits()
        make_user()
        api = authenticated()

        # Comfortably past the anonymous limit, from the same address that
        # limit would have counted.
        statuses = {api.get(reverse("auth:me")).status_code for _ in range(ANON_LIMIT + 5)}

        assert statuses == {200}
        assert api.get(reverse("auth:me"))["RateLimit-Limit"] == "300"

    def test_the_allowance_belongs_to_the_user_and_not_to_the_address(self, db, limits):
        # Small enough to exhaust in a test; the arithmetic is what is being
        # checked, not the number.
        limits(user="3/min")
        make_user("first@example.com")
        make_user("second@example.com")

        first = authenticated("first@example.com")
        second = authenticated("second@example.com")
        for _ in range(3):
            first.get(reverse("auth:me"))

        assert first.get(reverse("auth:me")).status_code == 429
        # Same address, same office, different person: an office behind one NAT
        # address must not share one allowance.
        assert second.get(reverse("auth:me")).status_code == 200

    def test_the_same_user_from_two_addresses_shares_one_allowance(self, db, limits):
        limits(user="3/min")
        make_user()

        desk = authenticated(REMOTE_ADDR="203.0.113.20")
        phone = authenticated(REMOTE_ADDR="203.0.113.21")
        for _ in range(3):
            desk.get(reverse("auth:me"))

        # A technician moving between wifi and mobile data does not get a fresh
        # allowance for each, and neither does a script rotating addresses.
        assert phone.get(reverse("auth:me")).status_code == 429


class TestWhoTheCallerIsTakenToBe:
    """The address the counter is keyed on, when a proxy is in front."""

    def test_a_forged_forwarded_header_does_not_buy_a_fresh_bucket(self, db, limits, settings):
        # Zero trusted proxies, which is this deployment's local shape: the
        # header is the caller's own writing and means nothing.
        settings.TRUSTED_PROXY_COUNT = 0
        limits()
        api = APIClient()
        for attempt in range(ANON_LIMIT):
            api.post(reverse("auth:refresh"), HTTP_X_FORWARDED_FOR=f"10.0.0.{attempt}")

        refused = api.post(reverse("auth:refresh"), HTTP_X_FORWARDED_FOR="10.0.0.99")

        # Twenty different forged addresses, one real caller, one bucket.
        assert refused.status_code == 429

    def test_behind_one_proxy_the_caller_is_the_entry_that_proxy_appended(
        self, db, limits, settings
    ):
        settings.TRUSTED_PROXY_COUNT = 1
        limits()
        api = APIClient(REMOTE_ADDR="127.0.0.1")
        for _ in range(ANON_LIMIT):
            api.post(reverse("auth:refresh"), HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.5")

        # Same real client, a different lie in front of it.
        refused = api.post(reverse("auth:refresh"), HTTP_X_FORWARDED_FOR="192.0.2.1, 203.0.113.5")
        # A genuinely different client, as the proxy would report it.
        other = api.post(reverse("auth:refresh"), HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.6")

        assert refused.status_code == 429
        assert other.status_code == 422

    @pytest.mark.parametrize(
        ("trusted", "forwarded", "expected"),
        [
            pytest.param(0, "1.2.3.4", "127.0.0.1", id="nothing in front, header ignored"),
            pytest.param(1, "1.2.3.4, 203.0.113.5", "203.0.113.5", id="one proxy, right-most"),
            pytest.param(2, "1.2.3.4, 203.0.113.5, 10.0.0.1", "203.0.113.5", id="two proxies"),
            pytest.param(1, "", "127.0.0.1", id="no header at all"),
            pytest.param(1, "not-an-address", "127.0.0.1", id="not an address"),
            pytest.param(1, "203.0.113.5:41234", "127.0.0.1", id="address with a port"),
            pytest.param(2, "203.0.113.5", "203.0.113.5", id="shorter than the count"),
        ],
    )
    def test_the_address_is_read_from_the_trusted_end(self, settings, trusted, forwarded, expected):
        settings.TRUSTED_PROXY_COUNT = trusted
        request = mock.Mock(META={"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": forwarded})

        assert client_ip(request) == expected


class TestWhatIsNotCounted:
    def test_the_health_endpoints_are_never_throttled(self, db, limits):
        limits(anon="1/min")
        client = Client()

        with mock.patch("core.views.storage.reachable", return_value=True):
            health = [client.get(reverse("health")).status_code for _ in range(25)]
            ready = [client.get(reverse("ready")).status_code for _ in range(25)]

        # Monitoring polls these every thirty seconds and the deploy waits on
        # /health for two and a half minutes. Throttling them would turn a busy
        # moment into a failed deploy and an instance pulled from rotation.
        assert health == [200] * 25
        assert ready == [200] * 25

    def test_the_health_endpoints_report_no_quota_because_they_have_none(self, db, limits):
        limits()

        response = Client().get(reverse("health"))

        assert "RateLimit-Limit" not in response
        assert "RateLimit-Remaining" not in response

    def test_a_request_with_no_token_to_a_closed_endpoint_is_refused_before_it_is_counted(
        self, db, limits
    ):
        limits(anon="1/min")
        api = APIClient()

        first = api.get(reverse("auth:me"))
        second = api.get(reverse("auth:me"))

        # DRF authenticates, then authorises, then throttles. Both are 401s and
        # neither reached the counter — documented rather than fixed, because
        # 8.13 puts the anonymous limit on the endpoints that admit anonymous
        # callers, and a 401 here costs a token parse and no query.
        assert (first.status_code, second.status_code) == (401, 401)


class TestTheGeocodingEndpoint:
    """Its own budget, and no second one on top of it."""

    def test_it_is_counted_against_the_provider_budget_and_not_the_general_one(self, db, limits):
        limits()
        make_user()
        api = authenticated()

        # No coordinates, so the request is refused by the query serializer and
        # the provider is never called. The throttle ran before any of that —
        # it is checked before the view body — which is exactly what is being
        # read off the response here.
        response = api.get(reverse("geocode-reverse"))

        assert response.status_code == 400
        # The quota reported is the geocoding scope's, not the API-wide one:
        # declaring throttle_classes on a view replaces the default rather than
        # stacking a second count on top of it.
        assert response["RateLimit-Limit"] == "60"
        assert response["RateLimit-Remaining"] == "59"

    def test_geocoding_does_not_spend_the_caller_s_general_allowance(self, db, limits):
        limits()
        make_user()
        api = authenticated()

        for _ in range(5):
            api.get(reverse("geocode-reverse"))

        # Five requests to the geocoder, and the caller's own allowance still
        # has only this one call against it.
        assert api.get(reverse("auth:me"))["RateLimit-Remaining"] == str(USER_LIMIT - 1)


class TestTheLoginLockout:
    """The per-account lockout of 7.4 and the throttle have to coexist.

    They answer different questions — one is about an account being attacked,
    the other about an address making too many requests — and either one
    swallowing the other would leave a hole where the swallowed rule was.
    """

    def test_five_wrong_passwords_still_reach_the_lockout(self, db, limits):
        limits()
        make_user()
        api = APIClient()
        login = reverse("auth:login")
        wrong = {"email": "user@example.com", "password": "not-the-password"}

        refusals = [api.post(login, wrong).data["error"]["code"] for _ in range(5)]
        locked = api.post(login, wrong)

        # Five attempts is well inside twenty a minute, so the account rule is
        # reached rather than hidden behind a 429 that says nothing about the
        # account being under attack.
        assert refusals == ["INVALID_CREDENTIALS"] * 5
        assert locked.status_code == 422
        assert locked.data["error"]["code"] == "ACCOUNT_LOCKED"

    def test_a_locked_account_does_not_buy_unlimited_attempts(self, db, limits):
        limits()
        make_user()
        api = APIClient()
        login = reverse("auth:login")
        wrong = {"email": "user@example.com", "password": "not-the-password"}

        statuses = [api.post(login, wrong).status_code for _ in range(ANON_LIMIT)]

        # Twenty attempts, the account locked after five, and the twenty-first
        # request from this address is refused by the throttle: the lockout does
        # not make the throttle unreachable any more than the reverse.
        assert statuses == [422] * ANON_LIMIT
        assert api.post(login, wrong).status_code == 429

    def test_the_lockout_is_per_account_and_the_throttle_is_per_address(self, db, limits):
        limits()
        make_user("first@example.com")
        make_user("second@example.com")
        api = APIClient()
        login = reverse("auth:login")

        for _ in range(5):
            api.post(login, {"email": "first@example.com", "password": "not-the-password"})
        other = api.post(login, {"email": "second@example.com", "password": PASSWORD})

        # One account locked, the other unaffected — an attacker cannot lock a
        # whole company out by failing against one address.
        assert other.status_code == 200


class TestHowItIsConfigured:
    def test_the_configured_rates_are_the_ones_the_specification_asks_for(self):
        from config.settings import base

        rates = base.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

        assert rates["anon"] == "20/min"
        assert rates["user"] == "300/min"
        assert base.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] == (
            "core.throttling.DefaultRateThrottle",
        )

    def test_the_counter_lives_in_the_cache_and_not_in_the_process(self, db, limits):
        limits()
        anonymous_call(APIClient())

        # This is the whole multi-worker question in one assertion. In
        # production the cache is Redis, so three gunicorn workers share this
        # key and the limit is twenty across all of them. Were it process
        # memory, each worker would keep its own list and the real limit would
        # be twenty times the number of workers.
        assert cache.get("throttle_anon_127.0.0.1") is not None

    def test_clearing_the_counter_restores_the_allowance(self, db, limits):
        limits(anon="1/min")
        api = APIClient()
        anonymous_call(api)
        assert anonymous_call(api).status_code == 429

        cache.clear()

        # Nothing outside the cache remembers the window, which is what makes
        # the shared store the single source of truth rather than a copy of one.
        assert anonymous_call(api).status_code == 422
