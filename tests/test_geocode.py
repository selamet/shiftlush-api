"""Reverse geocoding: what a dropped pin fills in, and what it refuses to.

The provider is mocked in every test here. Not for speed — because a suite that
reaches Nominatim fails when someone runs it on a train, spends a quota that is
capped at one request a second, and tests OpenStreetMap's data rather than this
code.

Everything below behaves identically on SQLite and on PostgreSQL, which is the
point of computing trigram similarity in Python: there is no second code path
for CI to be the only thing exercising. The one test that is PostgreSQL-specific
proves the Python and the database agree, and skips elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.address.matching import comparison_key, trigram_similarity, trigrams
from apps.address.models import District, Neighborhood, Province
from apps.users.services import register_company

SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")

PASSWORD = "correct-horse-battery"

#: Somewhere in Ataşehir. The coordinates never decide anything in these tests —
#: the mocked provider does — but a plausible pair keeps the intent readable.
ATASEHIR = {"lat": 40.9923, "lng": 29.1244}


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    # The locmem cache outlives a test, and both the geocoding responses and the
    # throttle counters live in it. One test's leftovers would answer another
    # test's lookup.
    cache.clear()


@pytest.fixture
def address_data(db) -> None:
    call_command("load_address_data", path=SAMPLE_DATA)


@pytest.fixture
def client(db) -> APIClient:
    register_company(
        legal_name="Test Ltd",
        display_name="Test",
        first_name="Test",
        last_name="User",
        email="user@example.com",
        password=PASSWORD,
    )
    api = APIClient()
    access = api.post(
        reverse("auth:login"), {"email": "user@example.com", "password": PASSWORD}
    ).data["access"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return api


class FakeResponse:
    """What `urlopen` hands back: a context manager with `read`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body[:amount] if amount else self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class UnreachableCache:
    """A cache backend that is down, in both directions."""

    def get(self, key: str, default: Any = None) -> Any:
        raise ConnectionError("cache is down")

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        raise ConnectionError("cache is down")


def nominatim(**address: Any) -> FakeResponse:
    """A reverse-geocoding answer shaped the way Nominatim shapes one."""
    body = {
        # Present so the tests prove these are dropped rather than forwarded.
        "place_id": 123456,
        "osm_id": 987654,
        "licence": "Data © OpenStreetMap contributors",
        "display_name": "somewhere",
        "address": {"country_code": "tr", **address},
    }
    return FakeResponse(json.dumps(body).encode())


def geocode(client: APIClient, **params: Any) -> Any:
    return client.get(reverse("geocode-reverse"), {**ATASEHIR, **params})


class TestAPointThatResolves:
    def test_all_three_levels_come_back_as_ids(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul",
                town="Ataşehir",
                neighbourhood="Küçükbakkalköy Mahallesi",
            ),
        ):
            response = geocode(client)

        assert response.status_code == 200
        body = response.data
        assert body["province"]["id"] == 34
        assert body["district"]["id"] == 3401
        assert body["neighborhood"]["id"] == 340102
        assert body["unmatched"] == []

    def test_the_abbreviation_and_the_spelled_out_word_are_one_place(self, client, address_data):
        # The table says "Küçükbakkalköy Mah.", OSM says "Mahallesi". Same
        # neighbourhood, and the confidence has to say so — a match reported at
        # 0.6 because of a word that means nothing would look like a guess.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul", town="Ataşehir", neighbourhood="Küçükbakkalköy Mahallesi"
            ),
        ):
            response = geocode(client)

        assert response.data["neighborhood"]["confidence"] == 1.0

    def test_a_spelling_that_is_merely_close_still_matches(self, client, address_data):
        # The provider splits "Bahçelievler" into two words; the table does not.
        # Nothing normalisation can fix, and exactly what the trigram threshold
        # exists for.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="Ankara", town="Çankaya", neighbourhood="Yukarı Bahçeli Evler Mahallesi"
            ),
        ):
            response = geocode(client)

        neighborhood = response.data["neighborhood"]
        assert neighborhood["id"] == 60103
        assert 0.4 <= neighborhood["confidence"] < 1.0

    def test_nothing_of_the_providers_own_payload_is_forwarded(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul", town="Ataşehir", neighbourhood="Barbaros Mahallesi"
            ),
        ):
            response = geocode(client)

        # A client that can read `osm_id` will read it, and the provider stops
        # being replaceable the moment one does.
        assert set(response.data) == {"province", "district", "neighborhood", "unmatched"}
        assert "osm_id" not in json.dumps(response.data)


class TestWhatItRefusesToGuess:
    """Specification 9.4: auto-filling the wrong neighbourhood is worse than
    leaving it blank. Each of these is a place a looser matcher would fill in."""

    def test_a_neighbourhood_we_do_not_have_is_left_empty(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul", town="Ataşehir", neighbourhood="Zümrütevler Mahallesi"
            ),
        ):
            response = geocode(client)

        assert response.data["neighborhood"] is None
        assert response.data["unmatched"] == ["neighborhood"]
        # The two levels that did match are still useful: the user picks one
        # field from a short list rather than starting from an empty form.
        assert response.data["province"]["id"] == 34
        assert response.data["district"]["id"] == 3401

    def test_it_does_not_reach_for_the_nearest_row(self, client, address_data):
        # District 3401 holds Barbaros, Küçükbakkalköy and İçerenköy. None of
        # them is Zümrütevler, and "closest of three" is not "correct".
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul", town="Ataşehir", neighbourhood="Zümrütevler Mahallesi"
            ),
        ):
            response = geocode(client)

        assert response.data["neighborhood"] is None

    def test_a_score_below_the_threshold_is_no_match_rather_than_a_low_one(
        self, client, address_data, settings
    ):
        settings.GEOCODING_MATCH_THRESHOLD = 0.99

        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="Ankara", town="Çankaya", neighbourhood="Yukarı Bahçeli Evler Mahallesi"
            ),
        ):
            response = geocode(client)

        # The same lookup matched at the default threshold. Raising the bar has
        # to empty the field, not lower the number reported next to it.
        assert response.data["neighborhood"] is None
        assert response.data["unmatched"] == ["neighborhood"]

    def test_two_equally_good_rows_are_a_tie_and_a_tie_is_nothing(self, client, address_data):
        # A tie is the case most likely to be wrong and least likely to be
        # noticed: whichever row wins looks exactly as confident as a real match.
        district = District.objects.get(pk=3401)
        Neighborhood.objects.create(
            id=340190, district=district, name="Yeni Mah.", name_normalized="yeni mah."
        )
        Neighborhood.objects.create(
            id=340191, district=district, name="Yeni Köyü", name_normalized="yeni koyu"
        )

        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="İstanbul", town="Ataşehir", neighbourhood="Yeni Mahallesi"
            ),
        ):
            response = geocode(client)

        assert response.data["neighborhood"] is None

    def test_a_level_whose_parent_is_unknown_is_unmatched_too(self, client, address_data):
        # Ataşehir is in the table, but under İstanbul. Matching it under a
        # province we could not place would be a coin flip: dozens of provinces
        # have a district by the same name.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(
                province="Bilecik", town="Ataşehir", neighbourhood="Barbaros Mahallesi"
            ),
        ):
            response = geocode(client)

        assert response.data["unmatched"] == ["province", "district", "neighborhood"]

    def test_a_pin_outside_turkey_matches_nothing(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=FakeResponse(
                json.dumps(
                    {"address": {"country_code": "bg", "state": "Burgas", "town": "Malko Tarnovo"}}
                ).encode()
            ),
        ):
            response = geocode(client, lat=41.9973, lng=27.5253)

        assert response.status_code == 200
        assert response.data["unmatched"] == ["province", "district", "neighborhood"]

    def test_a_point_with_no_address_at_all_is_an_empty_answer_not_an_error(
        self, client, address_data
    ):
        # A pin dropped in the Aegean. The provider answered; there is simply
        # nothing there, and an empty form is the correct response.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=FakeResponse(b'{"error": "Unable to geocode"}'),
        ):
            response = geocode(client, lat=38.5, lng=25.5)

        assert response.status_code == 200
        assert response.data["province"] is None
        assert response.data["unmatched"] == ["province", "district", "neighborhood"]


class TestWhenTheProviderFails:
    def test_an_http_error_answers_503(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            side_effect=HTTPError(
                url="https://nominatim.openstreetmap.org/reverse",
                code=429,
                msg="Too Many Requests",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            ),
        ):
            response = geocode(client)

        # 503 and not 500: nothing about the request was wrong, and the client's
        # move is to let the user pick the address by hand rather than to show
        # "something went wrong".
        assert response.status_code == 503
        assert response.data["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_a_timeout_answers_503_rather_than_hanging(self, client, address_data):
        with mock.patch("apps.address.geocoding.urlopen", side_effect=TimeoutError("timed out")):
            response = geocode(client)

        assert response.status_code == 503
        assert response.data["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_a_body_that_is_not_json_answers_503(self, client, address_data):
        # How a rate limit or a proxy in the way usually arrives: status 200,
        # content an HTML page.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=FakeResponse(b"<html><body>Too many requests</body></html>"),
        ):
            response = geocode(client)

        assert response.status_code == 503

    def test_a_failure_is_not_cached(self, client, address_data):
        with mock.patch("apps.address.geocoding.urlopen", side_effect=TimeoutError()):
            assert geocode(client).status_code == 503

        # Caching an outage would turn a five-second blip into a month of empty
        # answers for that point.
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(province="İstanbul", town="Ataşehir"),
        ):
            assert geocode(client).data["province"]["id"] == 34


class TestTheOutboundCall:
    def test_it_carries_a_timeout(self, client, address_data, settings):
        settings.GEOCODING_TIMEOUT_SECONDS = 3.5

        with mock.patch(
            "apps.address.geocoding.urlopen", return_value=nominatim(province="İstanbul")
        ) as urlopen:
            geocode(client)

        # Without it the socket waits forever and takes a worker with it.
        assert urlopen.call_args.kwargs["timeout"] == 3.5

    def test_it_identifies_itself(self, client, address_data, settings):
        settings.GEOCODING_USER_AGENT = "ShiftLush/1.0 (+https://example.test)"

        with mock.patch(
            "apps.address.geocoding.urlopen", return_value=nominatim(province="İstanbul")
        ) as urlopen:
            geocode(client)

        # Nominatim's usage policy requires this and blocks requests without it,
        # so it is a condition of running at all rather than a courtesy.
        request = urlopen.call_args.args[0]
        assert request.get_header("User-agent") == "ShiftLush/1.0 (+https://example.test)"

    def test_it_asks_for_turkish_names(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen", return_value=nominatim(province="İstanbul")
        ) as urlopen:
            geocode(client)

        # The address table stores Turkish names. Anything else and every
        # comparison downstream fails.
        assert "accept-language=tr" in urlopen.call_args.args[0].full_url

    def test_a_provider_url_that_is_not_https_is_refused_before_it_is_called(
        self, client, address_data, settings
    ):
        settings.GEOCODING_URL = "http://nominatim.openstreetmap.org/reverse"

        with mock.patch("apps.address.geocoding.urlopen") as urlopen:
            response = geocode(client)

        # Anything on the path could rewrite an address the user is about to
        # save, so this fails rather than downgrading quietly.
        assert response.status_code == 503
        assert urlopen.call_count == 0


class TestCaching:
    def test_the_same_point_is_looked_up_once(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(province="İstanbul", town="Ataşehir"),
        ) as urlopen:
            first = geocode(client)
            second = geocode(client)

        assert urlopen.call_count == 1
        assert first.data == second.data

    def test_a_pin_nudged_by_a_metre_shares_the_answer(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(province="İstanbul", town="Ataşehir"),
        ) as urlopen:
            geocode(client, lat=40.99231)
            geocode(client, lat=40.99232)

        # Four decimal places is about eleven metres — smaller than anything
        # this endpoint can resolve, so the second lookup would buy nothing.
        assert urlopen.call_count == 1

    def test_a_different_point_is_looked_up_again(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(province="İstanbul", town="Ataşehir"),
        ) as urlopen:
            geocode(client)
            geocode(client, lat=39.9208, lng=32.8541)

        assert urlopen.call_count == 2

    def test_nothing_there_is_cached_as_well(self, client, address_data):
        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=FakeResponse(b'{"error": "Unable to geocode"}'),
        ) as urlopen:
            geocode(client, lat=38.5, lng=25.5)
            geocode(client, lat=38.5, lng=25.5)

        # A pin dragged across the coastline produces a run of these. Leaving
        # them uncached would spend the quota fastest where it buys least.
        assert urlopen.call_count == 1

    def test_an_unreachable_cache_costs_a_lookup_and_not_the_answer(self, client, address_data):
        # Substituted for this module only. Patching the shared cache object
        # would break DRF's throttle too, and what is being checked here is the
        # geocoding path's own behaviour.
        with (
            mock.patch("apps.address.geocoding.cache", UnreachableCache()),
            mock.patch(
                "apps.address.geocoding.urlopen",
                return_value=nominatim(province="İstanbul", town="Ataşehir"),
            ),
        ):
            response = geocode(client)

        # A cache we cannot read means paying full price for every lookup, not
        # refusing to answer.
        assert response.status_code == 200
        assert response.data["province"]["id"] == 34


class TestTheRequestItself:
    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({}, id="both missing"),
            pytest.param({"lat": 40.9923}, id="lng missing"),
            pytest.param({"lng": 29.1244}, id="lat missing"),
            pytest.param({"lat": "north", "lng": "east"}, id="not numbers"),
            pytest.param({"lat": 91, "lng": 29.1}, id="latitude off the planet"),
            pytest.param({"lat": 40.9, "lng": -181}, id="longitude off the planet"),
            pytest.param({"lat": "", "lng": ""}, id="empty"),
        ],
    )
    def test_a_point_that_cannot_exist_is_refused_before_the_provider_is_called(
        self, client, address_data, params
    ):
        with mock.patch("apps.address.geocoding.urlopen") as urlopen:
            response = client.get(reverse("geocode-reverse"), params)

        assert response.status_code == 400
        assert response.data["error"]["code"] == "VALIDATION_ERROR"
        assert urlopen.call_count == 0

    def test_anonymous_callers_are_refused(self, address_data):
        response = APIClient().get(reverse("geocode-reverse"), ATASEHIR)
        assert response.status_code == 401

    def test_the_endpoint_is_rate_limited(self, client, address_data, monkeypatch):
        # DRF binds the rate table to the throttle class at import, so the
        # settings fixture cannot reach it.
        monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"geocode": "1/min"})

        with mock.patch(
            "apps.address.geocoding.urlopen",
            return_value=nominatim(province="İstanbul", town="Ataşehir"),
        ):
            assert geocode(client).status_code == 200
            # A different point, so the cache cannot answer it and the call
            # would otherwise reach the provider.
            refused = geocode(client, lat=39.9208, lng=32.8541)

        # The cache bounds repeat lookups; nothing bounds a client walking the
        # coordinate space except this.
        assert refused.status_code == 429
        assert refused.data["error"]["code"] == "THROTTLED"


class TestTrigramSimilarity:
    """`pg_trgm`'s measure, computed in Python. The last test here is what
    keeps that claim honest."""

    def test_a_word_is_padded_front_and_back(self):
        # Two spaces in front, one behind — which is why a three-letter word
        # yields four trigrams rather than one, and why short names compare at
        # all.
        assert trigrams("cat") == {"  c", " ca", "cat", "at "}

    def test_identical_names_score_one(self):
        assert trigram_similarity("kadikoy", "kadikoy") == 1.0

    def test_names_with_nothing_in_common_score_zero(self):
        assert trigram_similarity("kadikoy", "zzz") == 0.0

    def test_an_empty_name_scores_zero_rather_than_dividing_by_it(self):
        assert trigram_similarity("", "kadikoy") == 0.0

    def test_the_abbreviation_would_score_above_the_threshold_unaided(self):
        # Stripping the generic word is what turns this into 1.0, but even
        # without it the pair clears 0.4 — so the threshold is not being held up
        # by the word list alone.
        assert trigram_similarity("barbaros mah", "barbaros mahallesi") > 0.4

    def test_two_different_neighbourhoods_score_below_it(self):
        assert trigram_similarity("zumrutevler", "kucukbakkalkoy") < 0.4

    def test_the_generic_words_are_dropped_before_comparing(self):
        assert comparison_key("Küçükbakkalköy Mah.") == "kucukbakkalkoy"
        assert comparison_key("Küçükbakkalköy Mahallesi") == "kucukbakkalkoy"
        assert comparison_key("Yeniköy Köyü") == "yenikoy"

    def test_a_name_that_is_nothing_but_a_generic_word_keeps_it(self):
        # Stripping everything would leave an empty key, which matches nothing
        # and matches it with confidence.
        assert comparison_key("Mahallesi") == "mahallesi"

    def test_merkez_is_a_name_and_not_a_suffix(self):
        # Dozens of districts have a Merkez. Treating it as noise would make
        # every one of them tie with every other.
        assert comparison_key("Merkez Mah.") == "merkez"

    # Not wrapped in a transaction, so that a CREATE EXTENSION the role is not
    # allowed to run leaves the connection usable and this test can skip rather
    # than error.
    @pytest.mark.django_db(transaction=True)
    def test_it_agrees_with_postgres(self):
        """The claim this module rests on, checked against the real thing.

        Skipped on SQLite, where the pure-Python path is the only path there is
        and there is nothing to compare against. `make test-pg` and CI are where
        this one earns its place.
        """
        if connection.vendor != "postgresql":
            pytest.skip("pg_trgm is PostgreSQL's; this run has no similarity() to compare with")

        with connection.cursor() as cursor:
            try:
                # The schema does not install it yet — the neighbourhood
                # typeahead is still a prefix match — so the test creates what
                # it needs. On a database where that is not allowed there is
                # nothing to compare against and nothing to prove.
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except Exception:
                pytest.skip("pg_trgm is not available to this role")

            # ASCII on purpose: comparison keys are folded by `core.text`
            # before they get here, so multibyte handling is not what this is
            # testing.
            pairs = [
                ("barbaros", "barbaros"),
                ("barbaros mah", "barbaros mahallesi"),
                ("kucukbakkalkoy", "kucuk bakkalkoy"),
                ("yukari bahcelievler", "yukari bahceli evler"),
                ("zumrutevler", "kucukbakkalkoy"),
                ("istanbul", "izmir"),
                ("merkez", "sisli merkez"),
                ("", "barbaros"),
            ]
            for left, right in pairs:
                cursor.execute("SELECT similarity(%s, %s)", [left, right])
                (expected,) = cursor.fetchone()
                assert trigram_similarity(left, right) == pytest.approx(expected, abs=1e-6), (
                    f"{left!r} vs {right!r}"
                )


class TestTheDataItRunsOn:
    def test_the_sample_holds_the_rows_these_tests_name(self, address_data):
        # Every assertion above turns on ids from the fixture CSVs. If a refresh
        # renumbers them, this is the test that says so.
        assert Province.objects.get(pk=34).name == "İstanbul"
        assert District.objects.get(pk=3401).name == "Ataşehir"
        assert Neighborhood.objects.get(pk=340102).name == "Küçükbakkalköy Mah."
        assert Neighborhood.objects.get(pk=60103).name == "Yukarı Bahçelievler Mah."
