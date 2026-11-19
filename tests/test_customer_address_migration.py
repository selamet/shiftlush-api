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
