"""Load the Turkish address dataset.

Idempotent, so it can be re-run when the yearly refresh lands. Written as a
command rather than a data migration on purpose: 50,000 rows in a migration
would replay on every test database creation and turn a one-second setup into a
minute.

`--if-missing` is what the container entrypoint uses. Without the data no
building, complex or customer can be created, so it has to load on a new
environment; with the data already there, re-importing fifty thousand rows on
every restart costs boot time and takes write locks on a table the running
application is reading. The flag is the difference between those two.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.address.models import District, Neighborhood, NeighborhoodType, Province
from core.text import normalize

# Inserting 50,000 rows one at a time takes minutes; in batches it takes
# seconds.
BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "Load provinces, districts and neighbourhoods from CSV."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--path",
            default=str(Path(__file__).resolve().parents[2] / "data"),
            help="Directory holding province.csv, district.csv and neighborhood.csv.",
        )
        parser.add_argument(
            "--if-missing",
            action="store_true",
            help="Do nothing if the tables already hold data. Used by the container entrypoint.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["if_missing"] and self._already_loaded():
            self.stdout.write("Address data already present; nothing to do.")
            return

        directory = Path(options["path"])
        if not directory.exists():
            raise CommandError(f"{directory} does not exist.")

        self._load(directory)

    def _already_loaded(self) -> bool:
        """Whether all three tables hold something.

        Existence checks rather than counts: the question is whether anything is
        there, and `LIMIT 1` answers it without reading fifty thousand rows.
        Three cheap queries is the whole cost this adds to a normal restart.

        A half-loaded database would defeat this, and cannot happen: `_load` is
        atomic, so a load that dies partway leaves the tables exactly as it
        found them. What the check cannot see is a *stale* dataset — the yearly
        refresh is therefore a deliberate run without the flag, not something
        that happens quietly on a deploy.
        """
        return (
            Province.objects.exists()
            and District.objects.exists()
            and Neighborhood.objects.exists()
        )

    @transaction.atomic
    def _load(self, directory: Path) -> None:
        self._load_provinces(directory / "province.csv")
        self._load_districts(directory / "district.csv")
        self._load_neighborhoods(directory / "neighborhood.csv")

    def _rows(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            raise CommandError(f"{path.name} not found in {path.parent}.")
        with path.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _load_provinces(self, path: Path) -> None:
        provinces = [
            Province(id=int(row["id"]), name=row["name"], name_normalized=normalize(row["name"]))
            for row in self._rows(path)
        ]
        Province.objects.bulk_create(
            provinces,
            update_conflicts=True,
            update_fields=["name", "name_normalized"],
            unique_fields=["id"],
        )
        self.stdout.write(f"provinces: {len(provinces)}")

    def _load_districts(self, path: Path) -> None:
        districts = [
            District(
                id=int(row["id"]),
                province_id=int(row["province_id"]),
                name=row["name"],
                name_normalized=normalize(row["name"]),
            )
            for row in self._rows(path)
        ]
        District.objects.bulk_create(
            districts,
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            update_fields=["province_id", "name", "name_normalized"],
            unique_fields=["id"],
        )
        self.stdout.write(f"districts: {len(districts)}")

    def _load_neighborhoods(self, path: Path) -> None:
        rows = self._rows(path)
        neighborhoods = [
            Neighborhood(
                id=int(row["id"]),
                district_id=int(row["district_id"]),
                name=row["name"],
                # Filled here with the same function the search uses. This is
                # the whole reason the column exists.
                name_normalized=normalize(row["name"]),
                postal_code=row.get("postal_code", "") or "",
                type=row.get("type") or NeighborhoodType.NEIGHBORHOOD,
            )
            for row in rows
        ]
        Neighborhood.objects.bulk_create(
            neighborhoods,
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            update_fields=["district_id", "name", "name_normalized", "postal_code", "type"],
            unique_fields=["id"],
        )
        self.stdout.write(f"neighborhoods: {len(neighborhoods)}")
        # bulk_create does not fire post_save, so nothing lands in the audit
        # log — which is correct here. Reference data is not a business record
        # and auditing 50,000 unchanged rows every year would bury the entries
        # that matter.
