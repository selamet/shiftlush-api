"""Limiting the address dataset to the provinces a deployment actually serves.

A firm works one province. The address tables held all 81, so every dropdown in
the product offered eighty provinces nobody would ever pick.

Deleting them by hand is the obvious fix and it is a trap, which is what most of
this module is about. `entrypoint.sh` runs `load_address_data --if-missing` on
every container start and that flag only asks whether the tables are non-empty,
so a manual delete *survives* — until the yearly refresh runs the command
without the flag, or an environment is built from nothing, and all 81 come back
with nothing to announce it. So the tests here are less about the narrowing than
about the ways it could quietly come undone.

`tests/test_address.py` covers the loader and the endpoints, and
`tests/test_address_bootstrap.py` covers the shipped dataset. Both run with no
scope set, which is the case that has to keep behaving exactly as it did.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.address.models import District, Neighborhood, Province
from apps.customers.models import Customer, CustomerType
from apps.properties.models import Building, BuildingType
from apps.users.services import register_company
from core.context import company_context

#: The three-province sample from tests/test_address.py: İstanbul (34) with five
#: districts and seven neighbourhoods, Ankara (6) with one and three, and İzmir
#: (35) with neither. Big enough to have something to leave behind.
SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")

ISTANBUL = 34
ANKARA = 6

#: A neighbourhood in Ankara, one province below. The row a record points at
#: when a test needs a reference the scope is about to orphan.
KIZILAY = 60101

PASSWORD = "correct-horse-battery"


def counts() -> tuple[int, int, int]:
    return (
        Province.objects.count(),
        District.objects.count(),
        Neighborhood.objects.count(),
    )


@pytest.fixture
def sample(db) -> None:
    """The sample dataset, loaded whole, the way an unscoped deployment has it."""
    call_command("load_address_data", path=SAMPLE_DATA)


def make_firm(name: str, email: str, neighborhood_id: int | None = None) -> Building:
    """A company with one customer and one building, optionally with an address."""
    company, _ = register_company(
        legal_name=f"{name} Ltd",
        display_name=name,
        first_name="F",
        last_name="Owner",
        email=email,
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name=f"{name} customer"
        )
        return Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="Test",
            neighborhood_id=neighborhood_id,
        )


class TestTheScopeDecidesWhatIsLoaded:
    def test_an_unset_scope_still_loads_the_whole_country(self, sample):
        # The default, and the reason it is the default: development and CI
        # never set the variable, and CI builds an environment from nothing on
        # every run. Anything else here would have made this change a migration
        # for every existing deployment.
        assert counts() == (3, 6, 10)

    def test_a_scope_loads_only_what_it_names(self, db, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)
        assert Province.objects.get().name == "İstanbul"

    def test_the_lower_levels_follow_the_province(self, db, settings):
        # Districts and neighbourhoods are not filtered against the scope
        # directly — they are filtered against what survived the level above.
        # A neighbourhood whose district was dropped has to go with it, or the
        # picker offers a neighbourhood that resolves to nothing.
        settings.ADDRESS_PROVINCES = [ANKARA]

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 1, 3)
        assert District.objects.get().name == "Çankaya"

    def test_an_unknown_province_code_is_refused(self, db, settings):
        # The failure this prevents is the quiet one: a typo matches no row, so
        # nothing loads, and the API comes up with empty address tables — which
        # looks like a data problem rather than a character in `.env`.
        settings.ADDRESS_PROVINCES = [99]

        with pytest.raises(CommandError, match="99"):
            call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (0, 0, 0)

    def test_the_default_is_the_whole_country(self, settings):
        # Guards the default itself. A deployment's province is configuration;
        # committing one here would put a single firm's scope in everybody's
        # image.
        from django.conf import settings as configured

        assert configured.ADDRESS_PROVINCES == []


class TestARefreshCannotWidenTheScope:
    """The failure that makes this a setting rather than a one-off delete."""

    def test_a_full_run_removes_what_is_no_longer_in_scope(self, sample, settings):
        # The upgrade path: a deployment carrying all 81 provinces, whose owner
        # has just decided it serves one.
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)

    def test_the_yearly_refresh_does_not_put_them_back(self, sample, settings):
        # DEPLOYMENT.md calls for a bare `load_address_data` after a data
        # refresh, and that is exactly the command that used to reload all 81.
        # It reads the scope like every other path, so it cannot.
        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", path=SAMPLE_DATA)

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)

    def test_narrowing_is_idempotent(self, db, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", path=SAMPLE_DATA)
        before = counts()

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == before

    def test_widening_the_scope_again_loads_the_province_back(self, sample, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", path=SAMPLE_DATA)

        settings.ADDRESS_PROVINCES = [ISTANBUL, ANKARA]
        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (2, 6, 10)


class TestRecordsBlockTheRemoval:
    """A province a record points at is not the command's to delete.

    Every foreign key into `address.Neighborhood` is `on_delete=PROTECT`, so the
    database would refuse this in any case. That is the backstop and not the
    behaviour: `ProtectedError` names one row's primary key and leaves whoever
    ran the command to work out which province, which table and whose building.
    These tests are about refusing first, by name, with the tables untouched.
    """

    def test_a_building_in_the_province_stops_the_command(self, sample, settings):
        make_firm("Firm", "owner@example.com", neighborhood_id=KIZILAY)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        with pytest.raises(CommandError, match=r"properties\.Building"):
            call_command("load_address_data", path=SAMPLE_DATA)

    def test_nothing_is_deleted_when_it_refuses(self, sample, settings):
        make_firm("Firm", "owner@example.com", neighborhood_id=KIZILAY)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        with pytest.raises(CommandError):
            call_command("load_address_data", path=SAMPLE_DATA)

        # Not "most of Ankara survived": the whole run is one transaction, so a
        # refusal rolls the refreshed rows back with it rather than leaving a
        # half-reconciled database behind a message saying the command failed.
        assert counts() == (3, 6, 10)

    def test_the_blocked_record_still_resolves_its_address(self, sample, settings):
        building = make_firm("Firm", "owner@example.com", neighborhood_id=KIZILAY)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        with pytest.raises(CommandError):
            call_command("load_address_data", path=SAMPLE_DATA)

        found = Building.unscoped.select_related("neighborhood__district__province").get(
            id=building.id
        )
        assert found.neighborhood is not None
        assert found.neighborhood.district.province.name == "Ankara"

    def test_another_company_s_record_blocks_it_too(self, sample, settings):
        # The check runs outside a request, where `Building.objects` — the
        # tenant manager — has no company in context and answers `none()`. A
        # check written against it would report a clean table and hand the
        # delete to PROTECT after all, which is the ugly error this exists to
        # avoid. Nothing here opens a company context, so a CommandError rather
        # than a ProtectedError is the whole assertion.
        make_firm("Other", "other@example.com", neighborhood_id=KIZILAY)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        with pytest.raises(CommandError, match=r"properties\.Building"):
            call_command("load_address_data", path=SAMPLE_DATA)

    def test_a_soft_deleted_record_blocks_it_too(self, sample, settings):
        # A soft-deleted building is hidden, not gone: the row is still there
        # and still holds the foreign key. Deleting its neighbourhood would take
        # the history it exists to preserve, and PROTECT would refuse anyway.
        building = make_firm("Firm", "owner@example.com", neighborhood_id=KIZILAY)
        with company_context(building.company_id):
            Building.objects.filter(id=building.id).delete()
        assert Building.unscoped.get(id=building.id).is_deleted

        settings.ADDRESS_PROVINCES = [ISTANBUL]
        with pytest.raises(CommandError, match=r"properties\.Building"):
            call_command("load_address_data", path=SAMPLE_DATA)

    def test_a_record_inside_the_scope_does_not_block_anything(self, sample, settings):
        # The other half. A guard that refused on any reference at all would
        # make the scope unusable the moment the firm entered its first
        # building.
        make_firm("Firm", "owner@example.com", neighborhood_id=340101)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)

    def test_a_record_with_no_address_does_not_block_anything(self, sample, settings):
        # `neighborhood` is nullable and field staff open records without it.
        make_firm("Firm", "owner@example.com", neighborhood_id=None)
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)


class TestTheContainerBootPath:
    """`--if-missing`, which runs on every container start and never deletes."""

    def test_it_loads_only_the_scope_into_an_empty_database(self, db, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        assert counts() == (1, 5, 7)

    def test_it_does_not_delete_what_is_out_of_scope(self, sample, settings):
        # Fifty thousand rows and a possible refusal is not a thing to discover
        # during a container start. The boot path only ever makes the API
        # usable; the pruning half is a deliberate run.
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        assert counts() == (3, 6, 10)

    def test_it_says_so_when_the_table_is_wider_than_the_scope(self, sample, settings, capsys):
        settings.ADDRESS_PROVINCES = [ISTANBUL]

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        # Silence would leave the mismatch to be found by a user scrolling past
        # a province the firm does not work.
        assert "outside ADDRESS_PROVINCES" in capsys.readouterr().err

    def test_it_loads_a_scope_the_database_has_never_heard_of(self, db, settings):
        # The case the old check got wrong. A box whose tables are full of
        # Ankara and whose `.env` says İstanbul is "already loaded" by the
        # question "is anything there", and has nothing usable by the only
        # question that matters — every dropdown in the product comes back
        # empty, on a deployment that reported a clean boot.
        settings.ADDRESS_PROVINCES = [ANKARA]
        call_command("load_address_data", path=SAMPLE_DATA)

        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        assert Province.objects.filter(id=ISTANBUL).exists()
        # Added, not swapped: the boot path does not delete, so Ankara is still
        # there and the warning above is what asks for it to be dealt with.
        assert Province.objects.filter(id=ANKARA).exists()

    def test_a_restart_inside_the_scope_still_changes_nothing(self, db, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", path=SAMPLE_DATA)
        Province.objects.filter(id=ISTANBUL).update(name="Edited By Hand")

        call_command("load_address_data", "--if-missing", path=SAMPLE_DATA)

        # Skipped, not re-imported: the upsert would have restored the name.
        # This is what keeps a restart off the write path.
        assert Province.objects.get(id=ISTANBUL).name == "Edited By Hand"


class TestTheEndpointReportsTheScope:
    def test_the_province_list_holds_only_what_is_loaded(self, db, settings):
        settings.ADDRESS_PROVINCES = [ISTANBUL]
        call_command("load_address_data", path=SAMPLE_DATA)
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

        response = api.get(reverse("province-list"))

        assert response.status_code == 200
        assert [row["name"] for row in response.data] == ["İstanbul"]
