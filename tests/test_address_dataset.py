"""The committed address data itself.

Not the loader and not the endpoints — the three CSV files. They are refreshed
once a year from an upstream source, and a refresh that silently returns half
the country, or renumbers everything, or loses the postal codes, would otherwise
be a diff too large for anyone to read closely and too important to skim.

These parse the files directly rather than loading them, so they cost
milliseconds and can say something about all fifty thousand rows.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent.parent / "apps" / "address" / "data"


def rows(name: str) -> list[dict[str, str]]:
    with (DATA / f"{name}.csv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def provinces() -> list[dict[str, str]]:
    return rows("province")


@pytest.fixture(scope="module")
def districts() -> list[dict[str, str]]:
    return rows("district")


@pytest.fixture(scope="module")
def settlements() -> list[dict[str, str]]:
    return rows("neighborhood")


class TestItIsTheWholeCountry:
    def test_there_are_eighty_one_provinces(self, provinces):
        # Not "about eighty": the number is fixed by law and changes when a law
        # changes, which is exactly the moment a test should notice.
        assert len(provinces) == 81

    def test_province_ids_are_plate_codes(self, provinces):
        by_name = {row["name"]: int(row["id"]) for row in provinces}
        # The one identifier every Turkish adult knows by heart, and what the
        # rest of the system assumes when it stores a province id.
        assert by_name["İstanbul"] == 34
        assert by_name["Ankara"] == 6
        assert by_name["İzmir"] == 35
        assert sorted(by_name.values()) == list(range(1, 82))

    def test_the_district_count_is_plausible(self, districts):
        # 973 today. A range rather than the number, because districts are
        # created and merged, but a refresh returning 400 is not a merge.
        assert 900 <= len(districts) <= 1_000

    def test_the_settlement_count_is_plausible(self, settlements):
        assert 45_000 <= len(settlements) <= 60_000

    def test_a_known_district_has_its_known_neighbourhoods(self, provinces, districts, settlements):
        istanbul = next(row for row in provinces if row["name"] == "İstanbul")
        kadikoy = next(
            row
            for row in districts
            if row["province_id"] == istanbul["id"] and row["name"] == "Kadıköy"
        )
        names = {row["name"] for row in settlements if row["district_id"] == kadikoy["id"]}

        # Twenty-one, and these are four of them. A dataset that passes the
        # count checks and still has the wrong contents fails here.
        assert len(names) == 21
        assert {"Caferağa", "Osmanağa", "Fenerbahçe", "Suadiye"} <= names


class TestReferentialIntegrity:
    def test_every_district_belongs_to_a_province(self, provinces, districts):
        known = {row["id"] for row in provinces}
        orphans = [row["id"] for row in districts if row["province_id"] not in known]
        assert orphans == []

    def test_every_settlement_belongs_to_a_district(self, districts, settlements):
        known = {row["id"] for row in districts}
        orphans = [row["id"] for row in settlements if row["district_id"] not in known]
        assert orphans == []

    def test_ids_are_unique_within_each_file(self, provinces, districts, settlements):
        for name, table in (
            ("province", provinces),
            ("district", districts),
            ("settlement", settlements),
        ):
            ids = [row["id"] for row in table]
            # Neighbourhoods and villages arrive from upstream in separate id
            # spaces and 2,731 of them collide; the build script offsets the
            # villages. A duplicate here means that offset stopped being enough.
            assert len(ids) == len(set(ids)), f"{name} ids are not unique"


class TestTheFieldsScreensRelyOn:
    def test_nothing_is_unnamed(self, provinces, districts, settlements):
        for table in (provinces, districts, settlements):
            assert all(row["name"].strip() for row in table)

    def test_both_settlement_kinds_are_present(self, settlements):
        kinds = {row["type"] for row in settlements}
        # Villages are addresses too. A dataset with only neighbourhoods would
        # leave every rural lift unrecordable.
        assert kinds == {"neighborhood", "village"}
        villages = [row for row in settlements if row["type"] == "village"]
        assert len(villages) > 10_000

    def test_postal_codes_are_five_digits_when_present(self, settlements):
        codes = [row["postal_code"] for row in settlements if row["postal_code"]]
        assert len(codes) > 40_000
        malformed = [code for code in codes if not (len(code) == 5 and code.isdigit())]
        assert malformed == []
