"""Bringing an empty database up to a usable one.

`tests/test_address_dataset.py` checks the three CSV files. `tests/test_address.py`
checks the loader and the endpoints against a three-province sample. Neither
answers the question this module exists for: does the dataset the image actually
ships reach the database intact, and does the entrypoint's `--if-missing` call do
the right thing on both a new environment and a restart.

These load the real fifty thousand rows and so cost seconds rather than
milliseconds. That is the price of testing the thing that was broken — a fresh
deployment whose address tables are empty, where creating a building, a complex
or a customer fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.management import call_command

from apps.address.models import District, Neighborhood, Province

#: The three-province sample from tests/test_address.py. Used for the
#: `--if-missing` cases, which are about the flag rather than about the size of
#: the dataset.
SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")


def counts() -> tuple[int, int, int]:
    return (
        Province.objects.count(),
        District.objects.count(),
        Neighborhood.objects.count(),
    )


def sample_counts() -> tuple[int, int, int]:
    """What the sample on disk holds, counted rather than written down here.

    The sample is shared with the demo-data tests and gains rows when those need
    a province they did not have. Numbers copied into an assertion turn that into
    an unrelated failure somewhere else, which is a poor way to learn that a
    fixture grew.
    """
    root = Path(SAMPLE_DATA)

    def rows(name: str) -> int:
        lines = (root / f"{name}.csv").read_text(encoding="utf-8").splitlines()[1:]
        return sum(1 for line in lines if line)

    return rows("province"), rows("district"), rows("neighborhood")


@pytest.fixture
def country(db) -> None:
    """The committed dataset, loaded the way a new environment loads it."""
    call_command("load_address_data")


class TestTheShippedDatasetReachesTheDatabase:
    def test_all_eighty_one_provinces_are_there(self, country):
        # Fixed by law. The CSV is checked separately; this is the assertion
        # that the CSV made it through the loader and into the table.
        assert Province.objects.count() == 81

    def test_the_lower_levels_arrive_whole(self, country):
        # Not a magic number: the number of rows in the file. A load that
        # silently drops a batch shows up here and nowhere else.
        assert District.objects.count() == 973
        assert Neighborhood.objects.count() == 50_437

    def test_a_restart_against_a_loaded_database_changes_nothing(self, country):
        before = counts()
        call_command("load_address_data")
        assert counts() == before


class TestTheReferencesHold:
    """province → district → neighbourhood, end to end.

    Without this chain the address picker returns a district that belongs to no
    province, or a neighbourhood the building form cannot resolve.
    """

    def test_every_district_points_at_a_province_that_exists(self, country):
        known = set(Province.objects.values_list("id", flat=True))
        referenced = set(District.objects.values_list("province_id", flat=True))
        assert referenced - known == set()
        # And the other way: a province with no districts means a load that
        # stopped early, or a province the district file has never heard of.
        assert referenced == known

    def test_every_neighbourhood_points_at_a_district_that_exists(self, country):
        known = set(District.objects.values_list("id", flat=True))
        referenced = set(Neighborhood.objects.values_list("district_id", flat=True))
        assert referenced - known == set()
        assert referenced == known

    def test_a_known_chain_resolves_all_the_way_up(self, country):
        # The generic checks above pass on a dataset that is internally
        # consistent and wrong. This one names a real place.
        neighborhood = Neighborhood.objects.select_related("district__province").get(
            name="Caferağa", district__name="Kadıköy"
        )
        assert neighborhood.district.name == "Kadıköy"
        assert neighborhood.district.province.name == "İstanbul"
        assert neighborhood.district.province_id == 34
        assert neighborhood.district.neighborhoods.count() == 21


class TestTheEntrypointFlag:
    """`load_address_data --if-missing`, which runs on every container start."""

    def test_it_loads_into_an_empty_database(self, db):
        assert counts() == (0, 0, 0)
        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)
        assert counts() == sample_counts()

    def test_it_does_nothing_when_the_data_is_already_there(self, db):
        call_command("load_address_data", path=SAMPLE_DATA)
        Province.objects.filter(id=34).update(name="Edited By Hand")

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        # Skipped, not re-imported: the upsert would have restored the name.
        # This is what keeps a restart off the fifty-thousand-row write path.
        assert Province.objects.get(id=34).name == "Edited By Hand"

    def test_it_reloads_when_one_level_is_missing(self, db):
        # A database migrated but never loaded, or one where the address tables
        # were truncated. Provinces alone are not "loaded".
        call_command("load_address_data", path=SAMPLE_DATA)
        Neighborhood.objects.all().delete()

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        assert Neighborhood.objects.count() == sample_counts()[2]

    def test_without_the_flag_it_still_refreshes(self, db):
        # The yearly refresh path. `--if-missing` must not become the only way
        # the command is ever called.
        call_command("load_address_data", path=SAMPLE_DATA)
        Province.objects.filter(id=34).update(name="Edited By Hand")

        call_command("load_address_data", path=SAMPLE_DATA)

        assert Province.objects.get(id=34).name == "İstanbul"
