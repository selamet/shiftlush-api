"""Load the Turkish address dataset, narrowed to the provinces a deployment serves.

Idempotent, so it can be re-run when the yearly refresh lands. Written as a
command rather than a data migration on purpose: 50,000 rows in a migration
would replay on every test database creation and turn a one-second setup into a
minute.

`--if-missing` is what the container entrypoint uses. Without the data no
building, complex or customer can be created, so it has to load on a new
environment; with the data already there, re-importing fifty thousand rows on
every restart costs boot time and takes write locks on a table the running
application is reading. The flag is the difference between those two.

**The scope is `settings.ADDRESS_PROVINCES`, and it is deliberately not a
command-line argument.** A firm works one province, and the picker should offer
one province. That could have been done by deleting eighty of them by hand --
and it would have appeared to work, because `--if-missing` only asks whether the
tables are non-empty, so the deletion survives every restart. It would then come
undone in silence at the yearly refresh, or on the next environment built from
nothing, both of which reload all 81. Reading the scope here, on every load
path, is what makes that impossible: there is no run of this command that can
widen the dataset past what the deployment asked for. A flag would have handed
the scope back to whoever typed the command.

Empty scope means the whole country, so an environment that never sets the
variable -- development, CI -- loads the same 81 provinces it always did.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Model

from apps.address.models import District, Neighborhood, NeighborhoodType, Province
from core.text import normalize

# Inserting 50,000 rows one at a time takes minutes; in batches it takes
# seconds.
BATCH_SIZE = 2000

#: How to reach a province id from each address model, for the reference check
#: below. Written as lookup paths so that check is a join rather than an `IN`
#: list of fifty thousand primary keys.
PROVINCE_PATHS: tuple[tuple[type[Model], str], ...] = (
    (Province, "id"),
    (District, "province_id"),
    (Neighborhood, "district__province_id"),
)


class Command(BaseCommand):
    help = "Load provinces, districts and neighbourhoods from CSV, within ADDRESS_PROVINCES."

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
        scope = frozenset(settings.ADDRESS_PROVINCES)

        if options["if_missing"] and self._already_loaded(scope):
            self.stdout.write("Address data already present; nothing to do.")
            self._report_out_of_scope(scope)
            return

        directory = Path(options["path"])
        if not directory.exists():
            raise CommandError(f"{directory} does not exist.")

        # The boot path never deletes. Narrowing the scope of a deployment that
        # already carries records is a decision with a blast radius -- fifty
        # thousand rows, and a refusal if any of them are spoken for -- and a
        # container start is the wrong moment to discover it. `--if-missing`
        # only ever makes the API usable; the pruning half is a deliberate run.
        self._load(directory, scope, prune=not options["if_missing"])

    def _already_loaded(self, scope: frozenset[int]) -> bool:
        """Whether the data *this deployment needs* is there.

        Without a scope this is the original question -- does each of the three
        tables hold something -- answered with existence checks rather than
        counts, because `LIMIT 1` settles it without reading fifty thousand
        rows.

        With a scope it has to be asked about the scope rather than about the
        table. A box whose tables are full of Ankara and whose `.env` says
        Erzurum has address data by the old check and none by the only one that
        matters: every dropdown in the product would come back empty. So the
        question becomes whether the scoped provinces are present, at a cost
        bounded by the size of the scope instead of the size of the table.

        A half-loaded database would defeat this, and cannot happen: `_load` is
        atomic, so a load that dies partway leaves the tables exactly as it
        found them. What the check cannot see is a *stale* dataset -- the yearly
        refresh is therefore a deliberate run without the flag, not something
        that happens quietly on a deploy.
        """
        if not scope:
            return (
                Province.objects.exists()
                and District.objects.exists()
                and Neighborhood.objects.exists()
            )

        if set(Province.objects.filter(id__in=scope).values_list("id", flat=True)) != scope:
            return False
        with_districts = set(
            District.objects.filter(province_id__in=scope)
            .values_list("province_id", flat=True)
            .distinct()
        )
        if with_districts != scope:
            return False
        return Neighborhood.objects.filter(district__province_id__in=scope).exists()

    def _report_out_of_scope(self, scope: frozenset[int]) -> None:
        """Say so, on the boot path, when the table is wider than the scope.

        This is the case the flag cannot settle by itself: the scoped data is
        all present, so there is nothing to load, but provinces the deployment
        no longer serves are still being offered in every dropdown. Silence
        here would leave the mismatch to be discovered by a user scrolling past
        Adana. It is a warning rather than an action because deleting fifty
        thousand rows during a container start is not something that should
        happen without being asked for.
        """
        if not scope:
            return

        extra = Province.objects.exclude(id__in=scope).count()
        if not extra:
            return

        self.stderr.write(
            self.style.WARNING(
                f"{extra} province(s) outside ADDRESS_PROVINCES are still loaded. "
                "Run `load_address_data` without --if-missing to remove them."
            )
        )

    @transaction.atomic
    def _load(self, directory: Path, scope: frozenset[int], prune: bool) -> None:
        """Upsert everything in scope, then remove everything outside it.

        One transaction, so the command either reconciles the tables fully or
        changes nothing at all. That matters for the refusal below: a run
        blocked by a building still pointing at Adana rolls the refreshed rows
        back with it, rather than leaving a half-reconciled database behind a
        message that says the command failed.
        """
        province_ids = self._load_provinces(directory / "province.csv", scope)
        district_ids = self._load_districts(directory / "district.csv", province_ids)
        self._load_neighborhoods(directory / "neighborhood.csv", district_ids)
        if prune:
            self._prune(scope)

    def _rows(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            raise CommandError(f"{path.name} not found in {path.parent}.")
        with path.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _load_provinces(self, path: Path, scope: frozenset[int]) -> frozenset[int]:
        rows = self._rows(path)
        known = {int(row["id"]) for row in rows}

        # A province code nobody has heard of is a typo in `.env`, and without
        # this the failure it causes is the quiet kind: nothing matches, nothing
        # loads, and the API comes up with empty address tables -- which reads
        # as a data problem rather than as the one-character configuration
        # mistake it is.
        unknown = sorted(scope - known)
        if unknown:
            raise CommandError(
                f"ADDRESS_PROVINCES names province(s) that are not in {path.name}: "
                f"{', '.join(str(code) for code in unknown)}. "
                "The value is a comma-separated list of licence-plate codes, 1-81."
            )

        wanted = scope or frozenset(known)
        provinces = [
            Province(id=int(row["id"]), name=row["name"], name_normalized=normalize(row["name"]))
            for row in rows
            if int(row["id"]) in wanted
        ]
        Province.objects.bulk_create(
            provinces,
            update_conflicts=True,
            update_fields=["name", "name_normalized"],
            unique_fields=["id"],
        )
        self.stdout.write(f"provinces: {len(provinces)}")
        return wanted

    def _load_districts(self, path: Path, province_ids: frozenset[int]) -> frozenset[int]:
        districts = [
            District(
                id=int(row["id"]),
                province_id=int(row["province_id"]),
                name=row["name"],
                name_normalized=normalize(row["name"]),
            )
            for row in self._rows(path)
            if int(row["province_id"]) in province_ids
        ]
        District.objects.bulk_create(
            districts,
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            update_fields=["province_id", "name", "name_normalized"],
            unique_fields=["id"],
        )
        self.stdout.write(f"districts: {len(districts)}")
        return frozenset(district.id for district in districts)

    def _load_neighborhoods(self, path: Path, district_ids: frozenset[int]) -> None:
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
            for row in self._rows(path)
            if int(row["district_id"]) in district_ids
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

    def _prune(self, scope: frozenset[int]) -> None:
        """Remove the provinces this deployment does not serve.

        The only thing in this command that deletes, and narrow on purpose.
        Rows *inside* the scope are still never removed: a district that
        upstream has dissolved stays, because the buildings pointing at it would
        otherwise have nowhere to be. What goes is only what the deployment has
        said it does not serve.
        """
        if not scope:
            return

        doomed = list(Province.objects.exclude(id__in=scope).order_by("id"))
        if not doomed:
            return

        self._refuse_if_referenced([province.id for province in doomed])

        districts = neighborhoods = 0
        for province in doomed:
            # A province at a time, rather than one delete across the whole
            # complement. Django's collector materialises the rows it is
            # deleting in order to honour the PROTECT relations, and doing that
            # for forty-nine thousand neighbourhoods at once builds an `IN` list
            # to match. Per province it is a few hundred.
            neighborhoods += Neighborhood.objects.filter(
                district__province_id=province.id
            ).delete()[0]
            districts += District.objects.filter(province_id=province.id).delete()[0]
        Province.objects.filter(id__in=[province.id for province in doomed]).delete()

        names = ", ".join(province.name for province in doomed[:5])
        if len(doomed) > 5:
            names += f" and {len(doomed) - 5} more"
        self.stdout.write(
            f"removed {len(doomed)} province(s), {districts} district(s), "
            f"{neighborhoods} neighbourhood(s): {names}"
        )

    def _refuse_if_referenced(self, province_ids: list[int]) -> None:
        """Stop before deleting anything a real record points at.

        The foreign keys into `address.Neighborhood` are all
        `on_delete=PROTECT`, so the database would refuse this anyway -- but it
        would refuse with a `ProtectedError` naming one row's primary key and
        leaving whoever ran the command to work out which province, which table
        and whose building. Asking first turns that into a sentence, and asking
        before the first `DELETE` means the answer arrives while the tables are
        still untouched.

        The counts go through `unscoped` wherever a model has it. `objects` on a
        `CompanyOwnedModel` is the tenant manager, and outside a request there
        is no company in context, so it answers `none()`: a check written
        against it would report zero references for a table full of them and
        hand the delete to PROTECT after all. Soft-deleted rows have to be
        counted for the same reason -- they still hold the foreign key, and the
        history they exist to preserve would go with the neighbourhood.
        """
        blocking: list[str] = []
        for model, path in PROVINCE_PATHS:
            for relation in model._meta.related_objects:
                related_model = relation.related_model
                if related_model._meta.app_label == "address":
                    continue
                manager = getattr(related_model, "unscoped", None) or related_model._default_manager
                lookup = f"{relation.field.name}__{path}__in"
                count: int = manager.filter(**{lookup: province_ids}).count()
                if count:
                    blocking.append(f"{related_model._meta.label}.{relation.field.name}: {count}")

        if not blocking:
            return

        raise CommandError(
            "Refusing to remove provinces that records still point at: "
            + "; ".join(sorted(blocking))
            + ". Move those records inside ADDRESS_PROVINCES first — nothing has been deleted."
        )
