"""The shape of the portfolio the demo generator produces.

Not "it wrote some rows" — `tests/test_demo_account.py` covers that the commands
run and what they refuse to touch. These are about whether the rows describe a
firm that could exist, because that is the whole job of demonstration data and
it is the part that was wrong: the generator used to take the first twenty
neighbourhoods in the table, which ordered by name across eighty-one provinces
gave one small maintenance firm customers in Balikesir, Izmir and Tokat at once.

The assertions are on the geography rather than on Erzurum by name. Which
province the data lands in is read off the address dataset — see
`core.demo._province` — so pinning the tests to one province would be testing
the fixture rather than the rule. The province *selection* has its own class at
the bottom, where each of the three routes into it is built explicitly.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.companies.models import Company
from apps.customers.models import Customer
from apps.elevators.models import Elevator
from apps.properties.models import Building, Complex
from core import demo
from core.context import company_context, system_context

#: The same three-province sample the account tests use.
SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")


@pytest.fixture
def seeded(db) -> Company:
    call_command("load_address_data", path=SAMPLE_DATA)
    call_command("seed_demo_data")
    with system_context():
        return Company.objects.get()


def write_dataset(root: Path, provinces: dict[int, str], places: dict[int, int]) -> str:
    """Build an address dataset on disk.

    `places` maps a province to how many neighbourhoods it gets, spread over
    three districts and three postal codes, which is enough shape for the
    district ranking to have something to rank.
    """
    root.mkdir(parents=True, exist_ok=True)
    rows: dict[str, tuple[list[str], list[list[Any]]]] = {
        "province.csv": (["id", "name"], [[code, name] for code, name in provinces.items()]),
        "district.csv": (["id", "province_id", "name"], []),
        "neighborhood.csv": (["id", "district_id", "name", "postal_code", "type"], []),
    }
    for code in provinces:
        for district in range(3):
            district_id = code * 100 + district
            rows["district.csv"][1].append([district_id, code, f"District {district_id}"])
            for place in range(places[code] // 3 + 1):
                rows["neighborhood.csv"][1].append(
                    [
                        district_id * 100 + place,
                        district_id,
                        f"Place {district_id}-{place}",
                        f"{code:02d}{district:01d}{place % 3:02d}",
                        "neighborhood",
                    ]
                )
    for name, (header, body) in rows.items():
        with (root / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(body)
    return str(root)


def place_of(row: Any) -> Any:
    """The neighbourhood on a building or a customer.

    `Building.neighborhood` is nullable because field staff create records
    before they know the address. The generator always sets it, and asserting
    that here is what lets the rest of these read as one expression each.
    """
    place = row.neighborhood
    assert place is not None
    return place


def province_of(row: Any) -> int:
    return int(place_of(row).district.province_id)


def district_of(row: Any) -> int:
    return int(place_of(row).district_id)


class TestOneFirmInOneTown:
    def test_every_building_is_in_the_same_province(self, seeded):
        """The defect, stated as an assertion.

        A van cannot cross four provinces in a morning, so a portfolio spread
        over them is not a portfolio — it is a bug that happens to render.
        """
        with company_context(seeded.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert buildings
        assert len({province_of(building) for building in buildings}) == 1

    def test_the_customers_are_in_the_same_province_as_their_buildings(self, seeded):
        with company_context(seeded.id):
            customers = list(
                Customer.objects.select_related("neighborhood__district").filter(
                    neighborhood__isnull=False
                )
            )
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert customers
        province = {province_of(building) for building in buildings}
        assert {province_of(customer) for customer in customers} == province

    def test_the_work_is_spread_over_more_than_one_district(self, seeded):
        """The other failure mode, and the reason this is not simply a filter.

        Putting every customer in one district would satisfy the test above and
        still be wrong: a firm whose entire book is on four streets is as
        implausible as one with customers in five provinces.
        """
        with company_context(seeded.id):
            districts = {
                district_of(building)
                for building in Building.objects.select_related("neighborhood")
            }

        assert len(districts) > 1
        assert len(districts) <= demo.SERVICE_AREA_DISTRICTS

    def test_every_district_holds_several_customers(self, seeded):
        """Which is what makes the district a service area rather than a spread.

        One customer per district would mean the districts were picked per
        customer; several per district means they were picked once, for the
        firm.
        """
        with company_context(seeded.id):
            customers = list(
                Customer.objects.select_related("neighborhood").filter(neighborhood__isnull=False)
            )

        by_district: dict[int, int] = {}
        for customer in customers:
            key = district_of(customer)
            by_district[key] = by_district.get(key, 0) + 1

        assert by_district
        assert min(by_district.values()) > 1

    def test_the_customers_are_not_all_on_one_street(self, seeded):
        with company_context(seeded.id):
            places = {
                building.neighborhood_id
                for building in Building.objects.filter(neighborhood__isnull=False)
            }

        assert len(places) > 1


class TestAnEstateIsOnePlace:
    def test_the_blocks_of_a_complex_share_its_address(self, seeded):
        """A, B and C of one estate are one address with three entrances.

        They used to draw a neighbourhood each, so a three-block estate was
        three blocks in three parts of town — which reads on the buildings
        screen as three unrelated records that happen to have the same name.
        """
        with company_context(seeded.id):
            estates = list(Complex.objects.prefetch_related("buildings"))

            assert estates
            for estate in estates:
                blocks = list(estate.buildings.all())
                assert blocks
                assert {block.neighborhood_id for block in blocks} == {estate.neighborhood_id}
                assert {block.street for block in blocks} == {estate.street}

    def test_the_blocks_are_numbered_apart(self, seeded):
        """One address, but not one door: each block keeps its own entrance."""
        with company_context(seeded.id):
            for estate in Complex.objects.prefetch_related("buildings"):
                numbers = [block.building_number for block in estate.buildings.all()]
                assert len(numbers) == len(set(numbers))


class TestTheRecordsMatchTheirBuildings:
    def test_registration_numbers_open_with_the_province_plate(self, seeded):
        """A lift's registration number carries the plate code of its province.

        It was hard-coded to 34, so every generated lift claimed to have been
        registered in Istanbul whatever the building's address said.
        """
        with company_context(seeded.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))
            numbers = list(Elevator.objects.values_list("registration_number", flat=True))

        plate = f"{province_of(buildings[0]):02d}"
        assert numbers
        assert all(number.startswith(f"{plate}-") for number in numbers)

    def test_a_lift_serves_no_more_floors_than_its_block_has(self, seeded):
        with company_context(seeded.id):
            lifts = list(Elevator.objects.select_related("building"))

        assert lifts
        for lift in lifts:
            floors = lift.building.floor_count
            assert floors is not None
            assert lift.stop_count is not None
            assert lift.stop_count <= floors


class TestWhereTheProvinceComesFrom:
    """The three routes, each built rather than assumed.

    A deployment whose address dataset is scoped to one province has already
    said which province it serves. That is the case a colleague's setting is
    about to create, and the first test is what makes the two fit together
    without this module knowing the setting exists.
    """

    def test_a_dataset_holding_one_province_decides_it(self, db, tmp_path):
        scoped = write_dataset(tmp_path / "scoped", {25: "Erzurum"}, {25: 12})
        call_command("load_address_data", path=scoped)
        call_command("seed_demo_data")

        with system_context():
            company = Company.objects.get()
        with company_context(company.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert {province_of(building) for building in buildings} == {25}

    def test_an_unscoped_dataset_falls_to_the_province_the_data_names(self, db, tmp_path):
        """And to it rather than to the biggest.

        Istanbul is given four times the coverage here precisely so that the
        rule below it — best covered wins — would pick the wrong answer if the
        name were not consulted first.
        """
        both = write_dataset(tmp_path / "both", {25: "Erzurum", 34: "Istanbul"}, {25: 12, 34: 48})
        call_command("load_address_data", path=both)
        call_command("seed_demo_data")

        with system_context():
            company = Company.objects.get()
        with company_context(company.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert {province_of(building) for building in buildings} == {25}

    def test_a_dataset_without_it_falls_to_the_best_covered_province(self, db, tmp_path):
        """CI builds an environment from nothing, and a test fixture holds three
        provinces that need not include the one the demo data was written for.
        Neither may end in an exception: the point of the fallback is that any
        database with address data in it can be filled.
        """
        elsewhere = write_dataset(
            tmp_path / "elsewhere", {6: "Ankara", 34: "Istanbul"}, {6: 12, 34: 48}
        )
        call_command("load_address_data", path=elsewhere)
        call_command("seed_demo_data")

        with system_context():
            company = Company.objects.get()
        with company_context(company.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert {province_of(building) for building in buildings} == {34}

    def test_a_province_row_with_nothing_under_it_is_not_chosen(self, db, tmp_path):
        """The shape scoping a dataset is most likely to leave behind.

        Loading one province's districts while leaving all eighty-one province
        rows in place gives eighty provinces that exist and hold nothing. Erzurum
        is one of the empty ones here, so the lookup by name finds a row and
        would have built a service area with no districts in it.
        """
        scoped = write_dataset(tmp_path / "scoped", {6: "Ankara"}, {6: 24})
        call_command("load_address_data", path=scoped)
        bare = write_dataset(tmp_path / "bare", {25: "Erzurum"}, {25: 0})
        (Path(bare) / "district.csv").write_text("id,province_id,name\n", encoding="utf-8")
        (Path(bare) / "neighborhood.csv").write_text(
            "id,district_id,name,postal_code,type\n", encoding="utf-8"
        )
        call_command("load_address_data", path=bare)

        call_command("seed_demo_data")

        with system_context():
            company = Company.objects.get()
        with company_context(company.id):
            buildings = list(Building.objects.select_related("neighborhood__district"))

        assert buildings
        assert {province_of(building) for building in buildings} == {6}

    def test_an_empty_dataset_is_reported_rather_than_crashed_into(self, db):
        assert demo.has_address_data() is False
