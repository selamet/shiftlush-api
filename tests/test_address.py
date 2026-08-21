"""Address lookup, and the Turkish search trap it exists to avoid."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.address.models import District, Neighborhood, Province
from apps.users.services import register_company

PASSWORD = "correct-horse-battery"


@pytest.fixture
def address_data(db) -> None:
    call_command("load_address_data")


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


class TestLoader:
    def test_loads_all_three_levels(self, address_data):
        assert Province.objects.count() == 3
        assert District.objects.count() == 6
        assert Neighborhood.objects.count() == 10

    def test_is_idempotent(self, address_data):
        # The dataset is refreshed about once a year; re-running must update
        # rather than duplicate or fail.
        call_command("load_address_data")
        assert Neighborhood.objects.count() == 10

    def test_normalised_column_is_filled(self, address_data):
        istanbul = Province.objects.get(name="İstanbul")
        assert istanbul.name_normalized == "istanbul"


class TestTurkishSearch:
    """The reason `name_normalized` exists at all."""

    def test_lowercase_ascii_finds_the_dotted_capital(self, client, address_data):
        # A user types "sisli"; the record says "Şişli Merkez Mah.". Without
        # normalisation this returns nothing and looks like missing data.
        district = District.objects.get(name="Şişli")
        response = client.get(
            reverse("neighborhood-list"), {"district": district.id, "search": "sisli"}
        )
        assert response.status_code == 200
        assert [row["name"] for row in response.data] == ["Şişli Merkez Mah."]

    def test_dotless_i_matches(self, client, address_data):
        district = District.objects.get(name="Çankaya")
        response = client.get(
            reverse("neighborhood-list"), {"district": district.id, "search": "kizilay"}
        )
        assert [row["name"] for row in response.data] == ["Kızılay Mah."]

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("kucukbakkalkoy", "Küçükbakkalköy Mah."),
            ("KÜÇÜKBAKKALKÖY", "Küçükbakkalköy Mah."),
            ("küçük", "Küçükbakkalköy Mah."),
        ],
    )
    def test_any_spelling_of_the_query_works(self, client, address_data, query, expected):
        district = District.objects.get(name="Ataşehir")
        response = client.get(
            reverse("neighborhood-list"), {"district": district.id, "search": query}
        )
        assert expected in [row["name"] for row in response.data]

    def test_ic_with_dotted_capital_in_the_record(self, client, address_data):
        district = District.objects.get(name="Ataşehir")
        response = client.get(
            reverse("neighborhood-list"), {"district": district.id, "search": "icerenkoy"}
        )
        assert [row["name"] for row in response.data] == ["İçerenköy Mah."]


class TestListingRules:
    def test_neighbourhoods_need_a_district(self, client, address_data):
        # Around 50,000 rows nationwide. Serving them unfiltered would be a
        # payload nobody can use and a query nobody should run.
        assert client.get(reverse("neighborhood-list")).data == []

    def test_a_single_character_returns_nothing(self, client, address_data):
        district = District.objects.get(name="Ataşehir")
        response = client.get(
            reverse("neighborhood-list"), {"district": district.id, "search": "k"}
        )
        assert response.data == []

    def test_districts_need_a_province(self, client, address_data):
        assert client.get(reverse("district-list")).data == []

    def test_provinces_are_served_whole(self, client, address_data):
        # 81 rows is a dropdown, not a search.
        assert len(client.get(reverse("province-list")).data) == 3

    def test_anonymous_callers_are_refused(self, address_data):
        assert APIClient().get(reverse("province-list")).status_code == 401
