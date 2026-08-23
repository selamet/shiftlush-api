"""The sign-in lockout of specification 7.4, and who it is allowed to affect.

Five attempts per fifteen minutes for the same **e-mail and address**, then a
fifteen minute lock. Both halves of that key matter, and only one of them is
about stopping an attacker.

A lockout keyed on the e-mail alone counts correctly and protects the wrong
thing. Five wrong passwords is a trivial thing to send, so a lock that follows
the account rather than the pair hands anyone who knows a registered address a
free and permanent way to keep its owner signed out — from anywhere, repeated
every fifteen minutes, at no cost and no risk. That is not a weaker version of
the control; it is the control turned around to point at the customer. So the
assertion this file exists for is the negative one:
`test_the_same_account_is_untouched_from_another_address`. Everything else here
is the specification's arithmetic, which was never the part that was wrong.

The address is resolved by `core.client_ip`, which reads the right-most trusted
`X-Forwarded-For` entry. That is load-bearing rather than tidy: a bucket keyed on
a value the caller writes is not a bucket, and a lockout that a header can step
out of enforces nothing at all.
"""

from __future__ import annotations

import time

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from apps.users.models import User
from apps.users.services import register_company

PASSWORD = "correct-horse-battery"
EMAIL = "owner@example.com"

#: Specification 7.4. Written out rather than imported from the code under test,
#: so that changing the number has to be done here too, in front of a reviewer.
MAX_ATTEMPTS = 5

WRONG = "not-the-password"

#: Two addresses in the documentation range. The whole file is about telling
#: them apart.
VICTIM = "203.0.113.10"
ATTACKER = "198.51.100.7"


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


def login(client: APIClient, *, email: str = EMAIL, password: str = PASSWORD, **extra: str):
    return client.post(reverse("auth:login"), {"email": email, "password": password}, **extra)


def fail(client: APIClient, times: int = 1, *, email: str = EMAIL, **extra: str) -> list[str]:
    """Sign in wrongly `times` times and report what each attempt was told."""
    return [
        login(client, email=email, password=WRONG, **extra).data["error"]["code"]
        for _ in range(times)
    ]


class TestTheLockItself:
    def test_five_failures_from_one_address_lock_that_pair(self, owner):
        api = APIClient(REMOTE_ADDR=VICTIM)

        refused = fail(api, MAX_ATTEMPTS)
        locked = login(api)  # the correct password, now

        # Five attempts are refused as wrong; the sixth is refused as locked,
        # and being right no longer helps. That last part is what makes it a
        # lock rather than a slower way to say no.
        assert refused == ["INVALID_CREDENTIALS"] * MAX_ATTEMPTS
        assert locked.status_code == 422
        assert locked.data["error"]["code"] == "ACCOUNT_LOCKED"

    def test_the_fifth_failure_is_the_one_that_locks_and_not_the_first(self, owner):
        api = APIClient(REMOTE_ADDR=VICTIM)

        fail(api, MAX_ATTEMPTS - 1)

        # Four is inside the allowance. A lock that bit on the second attempt
        # would pass "a lock happened" and be a different product — the person
        # who mistypes their password twice is the common case, not the attack.
        assert login(api).status_code == 200

    def test_a_successful_sign_in_clears_the_count(self, owner):
        api = APIClient(REMOTE_ADDR=VICTIM)

        fail(api, MAX_ATTEMPTS - 1)
        assert login(api).status_code == 200

        fail(api, MAX_ATTEMPTS - 1)

        # Eight failures in the window, and not locked: the successful sign-in
        # between them reset the count. Were the counter cumulative, the fifth
        # of these would have locked the pair and this would be 422 — which is
        # the shape of the bug where somebody who mistypes twice a day every day
        # is eventually locked out for no reason they can see.
        assert login(api).status_code == 200

    def test_the_lock_expires(self, owner, monkeypatch):
        # Imported inside the test on purpose. The lock's duration is a property
        # of the new counter, so this is the one test in the file that cannot be
        # run against the old account-column implementation, and a module-level
        # import would have taken the other tests down with it.
        from apps.users import lockout

        monkeypatch.setattr(lockout, "LOCKOUT_SECONDS", 0.4)
        api = APIClient(REMOTE_ADDR=VICTIM)

        fail(api, MAX_ATTEMPTS)
        assert login(api).data["error"]["code"] == "ACCOUNT_LOCKED"

        time.sleep(0.5)

        # Fifteen minutes in production, patched to a fraction of a second here.
        # The clock is not frozen because the expiry is the cache's own, and a
        # frozen clock would prove that the assertion was written rather than
        # that the key goes away.
        assert login(api).status_code == 200


class TestWhoTheLockAffects:
    def test_the_same_account_is_untouched_from_another_address(self, owner):
        attacker = APIClient(REMOTE_ADDR=ATTACKER)
        victim = APIClient(REMOTE_ADDR=VICTIM)

        fail(attacker, MAX_ATTEMPTS)

        # This is the entire point of the change. The attacker has locked the
        # pair they were failing against, and the owner of the account signs in
        # from their own desk as though nothing had happened.
        #
        # Keyed on the account alone, this is 422 ACCOUNT_LOCKED: a stranger who
        # knows one e-mail address holds the customer out of their own product
        # for as long as they care to keep typing, and the support call is
        # indistinguishable from a forgotten password.
        assert login(attacker).data["error"]["code"] == "ACCOUNT_LOCKED"
        assert login(victim).status_code == 200

    def test_two_accounts_behind_one_address_do_not_lock_each_other(self, owner, db):
        # The office-behind-one-NAT-address case, which is the cost of putting
        # the address in the key at all. It is paid by the *pair*, not by the
        # address: the e-mail is in the key too, so a colleague fumbling their
        # own password cannot spend anybody else's allowance.
        register_company(
            legal_name="Second Ltd",
            display_name="Second",
            first_name="Other",
            last_name="Owner",
            email="colleague@example.com",
            password=PASSWORD,
        )
        office = APIClient(REMOTE_ADDR=VICTIM)

        fail(office, MAX_ATTEMPTS, email="colleague@example.com")

        assert login(office, email="colleague@example.com").data["error"]["code"] == (
            "ACCOUNT_LOCKED"
        )
        assert login(office).status_code == 200


class TestTheAddressCannotBeChosenByTheCaller:
    def test_a_forged_header_does_not_escape_the_bucket_with_no_proxy_in_front(self, owner):
        # TRUSTED_PROXY_COUNT is zero here, which is local development and the
        # suite: nothing is in front to have written the header, so it is
        # ignored outright and REMOTE_ADDR is the caller.
        api = APIClient(REMOTE_ADDR=VICTIM)

        codes = [
            login(api, password=WRONG, HTTP_X_FORWARDED_FOR=f"10.0.0.{n}").data["error"]["code"]
            for n in range(MAX_ATTEMPTS)
        ]
        forged = login(api, HTTP_X_FORWARDED_FOR="10.0.0.99")

        assert codes == ["INVALID_CREDENTIALS"] * MAX_ATTEMPTS
        assert forged.data["error"]["code"] == "ACCOUNT_LOCKED"

    def test_a_forged_header_does_not_escape_the_bucket_behind_a_proxy(self, owner, settings):
        # One proxy in front, which is the deployment. Caddy appends the address
        # it accepted the connection from, so the header reads
        # `<whatever the caller sent>, <the caller>` and only the right-hand end
        # was written by us.
        settings.TRUSTED_PROXY_COUNT = 1
        api = APIClient()

        codes = [
            login(
                api,
                password=WRONG,
                HTTP_X_FORWARDED_FOR=f"10.0.0.{n}, {ATTACKER}",
            ).data["error"]["code"]
            for n in range(MAX_ATTEMPTS)
        ]
        forged = login(api, HTTP_X_FORWARDED_FOR=f"10.0.0.99, {ATTACKER}")

        # A different forged entry on every attempt, and every one of them landed
        # in the same bucket. Read from the left — which is what DRF's own
        # throttle does, and what `apps/users/api/v1/views.py` used to do — each
        # of these is a bucket of its own and the lock is never reached: one
        # header a stranger controls, and the limit is not a limit.
        assert codes == ["INVALID_CREDENTIALS"] * MAX_ATTEMPTS
        assert forged.data["error"]["code"] == "ACCOUNT_LOCKED"

    def test_the_proxy_s_own_entry_is_what_separates_two_callers(self, owner, settings):
        settings.TRUSTED_PROXY_COUNT = 1
        api = APIClient()

        for _ in range(MAX_ATTEMPTS):
            login(api, password=WRONG, HTTP_X_FORWARDED_FOR=f"10.0.0.1, {ATTACKER}")

        # Same forged prefix, different real peer: still two callers, because
        # the entry that counts is the one the proxy appended.
        assert (
            login(api, HTTP_X_FORWARDED_FOR=f"10.0.0.1, {ATTACKER}").data["error"]["code"]
            == "ACCOUNT_LOCKED"
        )
        assert login(api, HTTP_X_FORWARDED_FOR=f"10.0.0.1, {VICTIM}").status_code == 200


class TestTheResponseIsNotAnOracle:
    def test_an_unregistered_address_is_counted_and_locked_exactly_like_a_registered_one(
        self, owner
    ):
        """The lock must not become a way to ask which addresses have accounts.

        `authenticate` is careful to answer `INVALID_CREDENTIALS` whether the
        e-mail is known or not, and counting only the known ones would give that
        care away at the sixth attempt: a registered address starts saying
        `ACCOUNT_LOCKED` and an unregistered one goes on saying
        `INVALID_CREDENTIALS` for ever. Six requests per address is a cheap
        oracle, and it enumerates a customer's whole staff list.
        """
        registered = [
            *fail(APIClient(REMOTE_ADDR=VICTIM), MAX_ATTEMPTS + 1),
        ]
        unregistered = [
            *fail(APIClient(REMOTE_ADDR=ATTACKER), MAX_ATTEMPTS + 1, email="nobody@example.com"),
        ]

        # Identical, attempt for attempt — including the sixth.
        assert registered == unregistered
        assert registered[-1] == "ACCOUNT_LOCKED"


class TestWhereTheCounterLives:
    def test_it_is_in_the_shared_cache_and_not_on_the_user_row(self, owner):
        api = APIClient(REMOTE_ADDR=VICTIM)

        fail(api, MAX_ATTEMPTS)
        assert login(api).data["error"]["code"] == "ACCOUNT_LOCKED"

        cache.clear()

        # This is the whole multi-worker question in one assertion. In production
        # the cache is Redis, so three gunicorn workers share the key and five
        # attempts means five across all of them. Were it process memory, each
        # worker would keep its own count and the real allowance would be five
        # times the number of workers — see the deviations table for the
        # measurement with real workers.
        #
        # Nothing outside the cache remembers the lock, which is also what makes
        # a support unlock one key rather than a database write.
        assert login(api).status_code == 200
