"""Migration 0009: the customer's own address moves into its address book.

Driven through `MigrationExecutor`, against the schema as it really was at 0008
and as it is at 0009, so the test reads the same historical models the
migration does — no custom managers, no tenant filter, no save hooks.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.address.models import District, Neighborhood, Province
from apps.users.services import register_company

PASSWORD = "correct-horse-battery"

BEFORE = ("customers", "0008_customer_address")
AFTER = ("customers", "0009_move_customer_address")
#: As a deploy runs it: the columns dropped too, so a reverse re-adds them empty
#: and only 0009's reverse fills them.
DROPPED = ("customers", "0010_drop_customer_address_columns")


def migrate_to(target: tuple[str, str]) -> Any:
    """Migrate `customers` to `target` and return the apps registry of that state."""
    # A fresh executor each time: the loader caches which migrations are applied.
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def migrate_to_latest() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def make_company(name: str) -> Any:
    company, _ = register_company(
        legal_name=f"{name} Ltd",
        display_name=name,
        first_name=name,
        last_name="Owner",
        email=f"{name.lower()}@example.com",
        password=PASSWORD,
    )
    return company


def addresses_of(apps: Any, customer_id: Any) -> list[dict[str, Any]]:
    address_model = apps.get_model("customers", "CustomerAddress")
    return list(
        address_model.objects.filter(customer_id=customer_id)
        .order_by("id")
        .values(
            "label",
            "is_billing",
            "neighborhood_id",
            "street",
            "building_number",
            "unit_number",
            "company_id",
        )
    )


@pytest.mark.django_db(transaction=True)
class TestMovingTheCustomerAddress:
    def test_the_address_moves_into_the_book_and_back(self):
        try:
            firm = make_company("Firm")
            other_firm = make_company("Other")
            province = Province.objects.create(id=34, name="İstanbul", name_normalized="istanbul")
            district = District.objects.create(
                id=3401, province=province, name="Kadıköy", name_normalized="kadikoy"
            )
            n = Neighborhood.objects.create(
                id=340101, district=district, name="Caferağa", name_normalized="caferaga"
            )

            old_apps = migrate_to(BEFORE)
            old_customer = old_apps.get_model("customers", "Customer")

            def customer(company: Any, legal_name: str, **values: Any) -> Any:
                return old_customer.objects.create(
                    company_id=company.id, type="corporate", legal_name=legal_name, **values
                )

            full = customer(
                firm,
                "Full",
                neighborhood_id=n.id,
                street="Ataturk Cd.",
                building_number="12",
                unit_number="3",
            )
            unit = customer(firm, "Unit", unit_number="5")
            empty = customer(firm, "Empty")
            # Another company's customer, soft-deleted: the migration has to see
            # past both the tenant filter and the soft-delete filter.
            deleted = customer(other_firm, "Deleted", street="Bagdat Cd.", is_deleted=True)

            new_apps = migrate_to(AFTER)
            address_model = new_apps.get_model("customers", "CustomerAddress")

            assert addresses_of(new_apps, full.id) == [
                {
                    "label": "Fatura adresi",
                    "is_billing": True,
                    "neighborhood_id": n.id,
                    "street": "Ataturk Cd.",
                    "building_number": "12",
                    "unit_number": "3",
                    "company_id": full.company_id,
                }
            ]
            assert len(addresses_of(new_apps, unit.id)) == 1
            assert addresses_of(new_apps, unit.id)[0]["unit_number"] == "5"
            assert addresses_of(new_apps, empty.id) == []

            moved = address_model.objects.get(customer_id=deleted.id)
            assert moved.street == "Bagdat Cd."
            assert moved.company_id == other_firm.id
            assert moved.is_deleted is True
            assert address_model.objects.get(customer_id=full.id).is_deleted is False

            # Back to 0008: the billing address's four values return to the
            # customer, the deleted customer's included.
            old_apps = migrate_to(BEFORE)
            old_customer = old_apps.get_model("customers", "Customer")
            rows = {
                row["legal_name"]: row
                for row in old_customer.objects.values(
                    "legal_name", "neighborhood_id", "street", "building_number", "unit_number"
                )
            }
            assert rows["Full"] == {
                "legal_name": "Full",
                "neighborhood_id": n.id,
                "street": "Ataturk Cd.",
                "building_number": "12",
                "unit_number": "3",
            }
            assert rows["Unit"]["unit_number"] == "5"
            assert rows["Empty"] == {
                "legal_name": "Empty",
                "neighborhood_id": None,
                "street": "",
                "building_number": "",
                "unit_number": "",
            }
            assert rows["Deleted"]["street"] == "Bagdat Cd."

            # Forward again: the reverse leaves the address rows in place, so a
            # second run must not add a second billing address beside them.
            again = migrate_to(AFTER)
            assert len(addresses_of(again, full.id)) == 1
            assert len(addresses_of(again, unit.id)) == 1
            assert len(addresses_of(again, deleted.id)) == 1
        finally:
            migrate_to_latest()

    def test_edits_made_while_rolled_back_win_on_the_re_apply(self):
        """Back at 0008 the old code edits the columns; those are the truth.

        Re-applying must carry what the office typed during the rollback into
        the book, not keep the row the first run wrote and drop the columns.
        """
        try:
            firm = make_company("Firm")
            other_firm = make_company("Other")

            old_apps = migrate_to(BEFORE)
            old_customer = old_apps.get_model("customers", "Customer")

            def customer(company: Any, legal_name: str, **values: Any) -> Any:
                return old_customer.objects.create(
                    company_id=company.id, type="corporate", legal_name=legal_name, **values
                )

            edited = customer(firm, "Edited", street="Ataturk Cd.", building_number="12")
            cleared = customer(firm, "Cleared", street="Old Cd.")
            noted = customer(firm, "Noted", street="Noted Cd.")
            rebilled = customer(firm, "Rebilled", street="First Cd.")
            deleted = customer(other_firm, "Deleted", street="Bagdat Cd.", is_deleted=True)

            new_apps = migrate_to(DROPPED)
            address_model = new_apps.get_model("customers", "CustomerAddress")
            # In the address book, before the rollback: a note only the book
            # can hold, and a billing row soft-deleted through the API.
            address_model.objects.filter(customer_id=noted.id).update(address_note="Arka kapı")
            address_model.objects.filter(customer_id=rebilled.id).update(is_deleted=True)

            old_apps = migrate_to(BEFORE)
            old_customer = old_apps.get_model("customers", "Customer")
            assert old_customer.objects.get(pk=rebilled.id).street == ""
            # What the old code writes while rolled back.
            old_customer.objects.filter(pk=edited.id).update(street="Moda Cd.")
            old_customer.objects.filter(pk=cleared.id).update(street="")
            old_customer.objects.filter(pk=noted.id).update(street="")
            old_customer.objects.filter(pk=rebilled.id).update(street="Second Cd.")
            old_customer.objects.filter(pk=deleted.id).update(street="Bagdat Cd. 2")

            again = migrate_to(DROPPED)
            address_model = again.get_model("customers", "CustomerAddress")

            def rows(customer_id: Any) -> list[Any]:
                return list(address_model.objects.filter(customer_id=customer_id).order_by("id"))

            # The edit wins, on the same row, and nothing is duplicated.
            [row] = rows(edited.id)
            assert (row.street, row.building_number, row.is_billing) == ("Moda Cd.", "12", True)
            assert row.is_deleted is False

            # Cleared and holding nothing else: soft-deleted, not left as an
            # empty billing row the API would refuse to save.
            [row] = rows(cleared.id)
            assert row.is_deleted is True and row.deleted_at is not None

            # Cleared but still holding a note: kept, live and billing, with the
            # four fields emptied to match the columns.
            [row] = rows(noted.id)
            assert row.is_deleted is False and row.is_billing is True
            assert (row.street, row.address_note) == ("", "Arka kapı")

            # Only a soft-deleted billing row: a new live one is created; the
            # old one stays as history and the billing slot holds one row.
            live = [r for r in rows(rebilled.id) if not r.is_deleted]
            assert [(r.street, r.is_billing) for r in live] == [("Second Cd.", True)]
            assert len(rows(rebilled.id)) == 2

            # A soft-deleted customer's row is updated and stays deleted with it.
            [row] = rows(deleted.id)
            assert (row.street, row.is_deleted) == ("Bagdat Cd. 2", True)
        finally:
            migrate_to_latest()
