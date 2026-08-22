"""Account self-service: changing a password, seeing and ending sessions.

These are the two endpoints where the security property is the feature. Most of
what is asserted here is a refusal — a wrong current password, somebody else's
session, a token that was revoked — because the happy path of both is a single
call and the interesting behaviour is everything around it.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.users.models import RefreshSession, User
from apps.users.services import register_company
from core.context import system_context

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "staple-battery-horse"
EMAIL = "owner@example.com"
OTHER_EMAIL = "other@example.com"

REFRESH_COOKIE = "shiftlush_refresh"


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


@pytest.fixture
def stranger(db) -> User:
    _, user = register_company(
        legal_name="Rival Elevator Ltd",
        display_name="Rival Elevator",
        first_name="Rival",
        last_name="Owner",
        email=OTHER_EMAIL,
        password=PASSWORD,
    )
    return user


def sign_in(email: str = EMAIL, password: str = PASSWORD) -> APIClient:
    """A client holding both halves of a session: bearer token and cookie.

    The cookie matters as much as the header here. It is the only thing that
    says *which* session is asking; the access token says only who.
    """
    client = APIClient()
    response = client.post(reverse("auth:login"), {"email": email, "password": password})
    assert response.status_code == 200, response.data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    return client


def change_password(client: APIClient, current: str = PASSWORD, new: str = NEW_PASSWORD):
    return client.post(
        reverse("auth:password-change"), {"current_password": current, "new_password": new}
    )


def sessions(client: APIClient):
    return client.get(reverse("auth:sessions"))


@pytest.fixture(autouse=True)
def _no_throttling(monkeypatch):
    """Off by default so every other test here is not fighting the rate limit.

    DRF binds the rate table to the throttle class at import, so the settings
    fixture cannot reach it. The test that proves the limit exists puts it back.
    """
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"password_change": None})


class TestPasswordChange:
    def test_the_new_password_is_the_one_that_works(self, owner):
        client = sign_in()

        assert change_password(client).status_code == 200

        assert (
            APIClient()
            .post(reverse("auth:login"), {"email": EMAIL, "password": NEW_PASSWORD})
            .status_code
            == 200
        )
        assert (
            APIClient()
            .post(reverse("auth:login"), {"email": EMAIL, "password": PASSWORD})
            .status_code
            == 422
        )

    def test_a_wrong_current_password_is_refused_by_name(self, owner):
        client = sign_in()

        response = change_password(client, current="not-the-password")

        # Not VALIDATION_ERROR. "What you typed is wrong" and "what you chose is
        # unacceptable" are different answers and the screen puts them in
        # different places — one under the current-password field, one under the
        # new one.
        assert response.status_code == 422
        assert response.data["error"]["code"] == "INVALID_CREDENTIALS"

    def test_a_wrong_current_password_changes_nothing(self, owner):
        client = sign_in()
        other_device = sign_in()

        change_password(client, current="not-the-password")

        owner.refresh_from_db()
        assert owner.check_password(PASSWORD)
        # And it is not a covert way to sign somebody else's devices out.
        assert other_device.post(reverse("auth:refresh")).status_code == 200

    @pytest.mark.parametrize(
        ("password", "accepted", "why"),
        [
            ("t7wq3z", True, "six characters, the floor"),
            ("t7wq3", False, "five, one under"),
            ("123456", False, "six, but on the common-password blocklist"),
            ("testowner", False, "the caller's own name"),
            ("owner@example.com", False, "the caller's own address"),
        ],
    )
    def test_the_new_password_meets_the_same_policy_as_registration(
        self, owner, password, accepted, why
    ):
        # The same table registration is held to, run against this endpoint. The
        # last two rows are the part that only passes because the serializer
        # names the caller: UserAttributeSimilarityValidator is configured
        # project-wide but permits anything at all when it is handed no user.
        response = change_password(sign_in(), new=password)

        if accepted:
            assert response.status_code == 200, why
        else:
            assert response.status_code == 400, why
            assert response.data["error"]["code"] == "VALIDATION_ERROR", why

    def test_the_session_making_the_change_survives_it(self, owner):
        client = sign_in()

        response = change_password(client)

        # The whole reason this endpoint exists rather than a reset link: the
        # device in the person's hand stays signed in.
        assert response.status_code == 200
        assert response.data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        assert client.get(reverse("auth:me")).status_code == 200
        # The cookie it now holds is a working one, not the dead one it arrived
        # with — the refresh token is rotated by the change like any other.
        assert client.post(reverse("auth:refresh")).status_code == 200

    def test_it_stays_one_session_in_the_list(self, owner):
        client = sign_in()
        before = sessions(client).data[0]

        change_password(client)

        after = sessions(client).data
        # Same device, never signed out, so the same session — not a second row
        # appearing in the settings screen for a password the user just changed.
        assert len(after) == 1
        assert after[0]["id"] == before["id"]
        assert after[0]["signed_in_at"] == before["signed_in_at"]
        assert after[0]["is_current"] is True

    def test_every_other_session_ends(self, owner):
        client = sign_in()
        phone = sign_in()

        change_password(client)

        # Leaving them alive would make the change decorative: the phone in the
        # taxi keeps refreshing on a credential the new password retired.
        refused = phone.post(reverse("auth:refresh"))
        assert refused.status_code == 422
        assert refused.data["error"]["code"] == "TOKEN_INVALID"

    def test_an_evicted_device_cannot_take_the_kept_session_down_with_it(self, owner):
        client = sign_in()
        phone = sign_in()

        change_password(client)
        phone.post(reverse("auth:refresh"))  # the phone notices, fifteen minutes later

        # A session that was ended has no live successor, so its token coming
        # back is expected rather than a replay. Reading it as one would sign
        # the user out of the device they deliberately kept.
        assert client.post(reverse("auth:refresh")).status_code == 200

    def test_the_callers_own_previous_token_is_dead_and_replaying_it_is_a_replay(self, owner):
        client = sign_in()
        superseded = client.cookies[REFRESH_COOKIE].value

        change_password(client)

        client.cookies[REFRESH_COOKIE] = superseded
        refused = client.post(reverse("auth:refresh"))
        assert refused.status_code == 422
        assert refused.data["error"]["code"] == "TOKEN_INVALID"
        # This chain *does* have a live successor, so this is the case the reuse
        # detection is for: two parties holding tokens from one live session.
        with system_context():
            assert not RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).exists()

    def test_it_is_rate_limited(self, owner, monkeypatch):
        monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"password_change": "1/hour"})
        client = sign_in()

        assert change_password(client, current="wrong-once").status_code == 422
        refused = change_password(client, current="wrong-twice")

        # Unthrottled this is a guessing oracle for anybody holding a stolen
        # access token: the difference between 422 and 200 is the answer.
        assert refused.status_code == 429
        assert refused.data["error"]["code"] == "THROTTLED"

    def test_anonymous_callers_are_refused(self, owner):
        assert change_password(APIClient()).status_code == 401

    def test_an_unknown_field_is_rejected(self, owner):
        response = sign_in().post(
            reverse("auth:password-change"),
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "user_id": "someone"},
        )
        assert response.status_code == 400


class TestSessionListing:
    def test_one_entry_per_device(self, owner):
        laptop = sign_in()
        sign_in()

        body = sessions(laptop).data

        assert len(body) == 2
        assert {entry["is_current"] for entry in body} == {True, False}

    def test_rotation_does_not_add_a_row(self, owner):
        client = sign_in()
        first = sessions(client).data[0]

        for _ in range(3):
            assert client.post(reverse("auth:refresh")).status_code == 200

        body = sessions(client).data
        # Four refresh_session rows exist by now. A listing built on rows rather
        # than on sessions would show four devices for one browser, and a month
        # of use would show three thousand.
        with system_context():
            assert RefreshSession.objects.filter(user=owner).count() == 4
        assert len(body) == 1
        assert body[0]["id"] == first["id"]
        assert body[0]["signed_in_at"] == first["signed_in_at"]
        # Sign-in time is fixed; last use moves with every rotation.
        assert body[0]["last_used_at"] > first["last_used_at"]
        assert body[0]["is_current"] is True

    def test_each_entry_carries_enough_to_recognise_the_device(self, owner):
        client = APIClient(HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")
        response = client.post(reverse("auth:login"), {"email": EMAIL, "password": PASSWORD})
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

        entry = sessions(client).data[0]

        assert set(entry) == {
            "id",
            "signed_in_at",
            "last_used_at",
            "expires_at",
            "user_agent",
            "ip_address",
            "is_current",
        }
        assert entry["user_agent"] == "Mozilla/5.0 (iPhone)"

    def test_exactly_one_entry_is_the_current_one(self, owner):
        laptop = sign_in()
        phone = sign_in()

        from_laptop = sessions(laptop).data
        from_phone = sessions(phone).data

        assert [entry["is_current"] for entry in from_laptop].count(True) == 1
        assert [entry["is_current"] for entry in from_phone].count(True) == 1
        # The two clients see the same two sessions and disagree about which is
        # theirs, which is the only way "sign out the others" can be unambiguous.
        current_for_laptop = next(e["id"] for e in from_laptop if e["is_current"])
        current_for_phone = next(e["id"] for e in from_phone if e["is_current"])
        assert current_for_laptop != current_for_phone

    def test_a_caller_sees_only_their_own_sessions(self, owner, stranger):
        mine = sign_in()
        sign_in(email=OTHER_EMAIL)

        body = sessions(mine).data

        # A session list is a record of where a person has been and on what.
        # Nobody else's belongs in this answer — not a colleague's, and not an
        # owner's view of a colleague's.
        assert len(body) == 1
        with system_context():
            assert RefreshSession.objects.filter(user=stranger).exists()

    def test_a_revoked_session_is_not_listed(self, owner):
        client = sign_in()
        phone = sign_in()
        with system_context():
            RefreshSession.objects.filter(user=owner).exclude(
                chain_id=_chain_of(client, owner)
            ).update(revoked_at=timezone.now())

        assert len(sessions(client).data) == 1
        assert phone.post(reverse("auth:refresh")).status_code == 422

    def test_an_expired_session_drops_out(self, owner):
        client = sign_in()
        phone = sign_in()
        with system_context():
            RefreshSession.objects.filter(user=owner).exclude(
                chain_id=_chain_of(client, owner)
            ).update(expires_at=timezone.now() - timezone.timedelta(seconds=1))

        # A thirty-day token that ran out is not a device the user has to do
        # anything about, and listing it as live would invite them to end
        # something that already ended.
        assert len(sessions(client).data) == 1
        assert phone.post(reverse("auth:refresh")).data["error"]["code"] == "TOKEN_EXPIRED"

    def test_anonymous_callers_are_refused(self, owner):
        assert sessions(APIClient()).status_code == 401


class TestSessionRevocation:
    def test_ending_a_session_stops_its_refresh_token(self, owner):
        laptop = sign_in()
        phone = sign_in()
        phone_id = next(e["id"] for e in sessions(phone).data if e["is_current"])

        assert laptop.delete(_revoke_url(phone_id)).status_code == 204

        refused = phone.post(reverse("auth:refresh"))
        assert refused.status_code == 422
        assert refused.data["error"]["code"] == "TOKEN_INVALID"
        assert len(sessions(laptop).data) == 1

    def test_the_session_that_did_the_ending_is_untouched(self, owner):
        laptop = sign_in()
        phone = sign_in()
        phone_id = next(e["id"] for e in sessions(phone).data if e["is_current"])

        laptop.delete(_revoke_url(phone_id))
        phone.post(reverse("auth:refresh"))  # the evicted device tries anyway

        # The point of the endpoint. If the evicted device's next refresh read as
        # a replay, ending the old phone would sign the laptop out too.
        assert laptop.post(reverse("auth:refresh")).status_code == 200

    def test_a_rotated_token_replayed_still_trips_reuse_detection(self, owner):
        client = sign_in()
        stolen = client.cookies[REFRESH_COOKIE].value
        assert client.post(reverse("auth:refresh")).status_code == 200  # the holder rotates

        client.cookies[REFRESH_COOKIE] = stolen
        assert client.post(reverse("auth:refresh")).status_code == 422

        # Unchanged from before this feature existed: a token superseded by a
        # rotation coming back means a copy is in circulation, and every session
        # goes.
        with system_context():
            assert not RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).exists()

    def test_another_users_session_cannot_be_revoked(self, owner, stranger):
        mine = sign_in()
        theirs = sign_in(email=OTHER_EMAIL)
        their_id = next(e["id"] for e in sessions(theirs).data if e["is_current"])

        response = mine.delete(_revoke_url(their_id))

        # 404, not 403. A 403 would confirm the id names a real session, which
        # is the whole of what guessing ids is for.
        assert response.status_code == 404
        assert response.data["error"]["code"] == "NOT_FOUND"
        assert theirs.post(reverse("auth:refresh")).status_code == 200

    def test_an_owner_cannot_revoke_a_colleagues_session_either(self, owner, db):
        from apps.users.models import Role

        with system_context():
            colleague = User.objects.create_user(
                email="tech@example.com",
                password=PASSWORD,
                company=owner.company,
                first_name="Field",
                last_name="Tech",
                role=Role.TECHNICIAN,
                is_email_verified=True,
            )
        boss = sign_in()
        theirs = sign_in(email=colleague.email)
        their_id = next(e["id"] for e in sessions(theirs).data if e["is_current"])

        # Administering a firm includes deactivating a leaver, which ends their
        # sessions. It does not include reading which devices they carry or
        # picking one off.
        assert boss.delete(_revoke_url(their_id)).status_code == 404
        assert len(sessions(boss).data) == 1
        assert theirs.post(reverse("auth:refresh")).status_code == 200

    def test_an_unknown_session_id_is_a_404(self, owner):
        assert (
            sign_in().delete(_revoke_url("0193f3aa-0000-7000-8000-000000000000")).status_code == 404
        )

    def test_ending_a_session_twice_is_a_404(self, owner):
        laptop = sign_in()
        phone = sign_in()
        phone_id = next(e["id"] for e in sessions(phone).data if e["is_current"])

        assert laptop.delete(_revoke_url(phone_id)).status_code == 204
        assert laptop.delete(_revoke_url(phone_id)).status_code == 404

    def test_a_caller_may_end_their_own_session(self, owner):
        client = sign_in()
        own_id = next(e["id"] for e in sessions(client).data if e["is_current"])

        response = client.delete(_revoke_url(own_id))

        assert response.status_code == 204
        # The cookie goes with it. Left behind, the browser would hold a dead
        # token and its next refresh would look like a replay.
        assert response.cookies[REFRESH_COOKIE].value == ""
        assert client.post(reverse("auth:refresh")).status_code == 422

    def test_anonymous_callers_are_refused(self, owner):
        assert (
            APIClient().delete(_revoke_url("0193f3aa-0000-7000-8000-000000000000")).status_code
            == 401
        )


class TestRevokeOthers:
    def test_it_ends_every_session_but_the_callers(self, owner):
        laptop = sign_in()
        phone = sign_in()
        tablet = sign_in()

        assert laptop.post(reverse("auth:sessions-revoke-others")).status_code == 204

        assert laptop.post(reverse("auth:refresh")).status_code == 200
        assert phone.post(reverse("auth:refresh")).status_code == 422
        assert tablet.post(reverse("auth:refresh")).status_code == 422

    def test_the_list_is_left_with_one_entry(self, owner):
        laptop = sign_in()
        sign_in()
        sign_in()

        laptop.post(reverse("auth:sessions-revoke-others"))

        body = sessions(laptop).data
        assert len(body) == 1
        assert body[0]["is_current"] is True

    def test_it_reaches_no_further_than_the_caller(self, owner, stranger):
        mine = sign_in()
        theirs = sign_in(email=OTHER_EMAIL)

        mine.post(reverse("auth:sessions-revoke-others"))

        assert theirs.post(reverse("auth:refresh")).status_code == 200

    def test_without_a_refresh_cookie_it_ends_everything(self, owner):
        laptop = sign_in()
        phone = sign_in()
        del laptop.cookies[REFRESH_COOKIE]

        assert laptop.post(reverse("auth:sessions-revoke-others")).status_code == 204

        # "All but this one" has no meaning when this one cannot be named. Of
        # the two ways to guess, leaving a session alive is the one that fails
        # the request the user actually made.
        assert phone.post(reverse("auth:refresh")).status_code == 422
        with system_context():
            assert not RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).exists()

    def test_anonymous_callers_are_refused(self, owner):
        assert APIClient().post(reverse("auth:sessions-revoke-others")).status_code == 401


def _revoke_url(session_id: str) -> str:
    return reverse("auth:session-revoke", kwargs={"session_id": session_id})


def _chain_of(client: APIClient, user: User):
    with system_context():
        from apps.users.services import _hash

        return RefreshSession.objects.get(
            user=user, token_hash=_hash(client.cookies[REFRESH_COOKIE].value)
        ).chain_id
