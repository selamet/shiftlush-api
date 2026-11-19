"""Move each customer's own address into its address book.

Until now a customer carried one address in four columns, which the web app
always presented as the billing address. Each customer with any of the four set
gets one `CustomerAddress` holding them, labelled and flagged as the billing
address. The next migration drops the columns.

Historical models carry none of the project's managers — `TenantManager` is not
`use_in_migrations` — so `objects` here is Django's plain manager: every company,
soft-deleted rows included, which is what has to move. `bulk_create` and
`bulk_update` send no signals, so the audit trail is not written for a schema move.
"""

from django.db import migrations
from django.utils import timezone

# The only customer columns the move reads or writes. Everything else, the
# encrypted `national_id` above all, stays unloaded so one undecryptable row
# cannot abort the migration.
CUSTOMER_COLUMNS = ("id", "company_id", "is_deleted", "deleted_at")
ADDRESS_FIELDS = ("neighborhood_id", "street", "building_number", "unit_number")
BILLING_LABEL = "Fatura adresi"


def _has_address(row):
    return row.neighborhood_id is not None or any(
        (getattr(row, field) or "").strip()
        for field in ("street", "building_number", "unit_number")
    )


def _current_billing(address_model):
    """Each customer's billing address as the columns at 0008 mirror it.

    The live one; for a soft-deleted customer without one, the one deleted with
    it. A live customer's soft-deleted billing row is history, not its address.
    Live rows sort first and win. Shared by both directions, so the reverse
    copies from exactly the row a re-apply writes back to.
    """
    chosen = {}
    billing = (
        address_model.objects.filter(is_billing=True)
        .select_related("customer")
        .only(
            "id",
            "customer_id",
            "is_billing",
            "is_deleted",
            "deleted_at",
            "updated_at",
            "neighborhood_id",
            "street",
            "building_number",
            "unit_number",
            "address_note",
            "customer__id",
            "customer__is_deleted",
        )
        .order_by("customer_id", "is_deleted", "-id")
    )
    for address in billing:
        if address.is_deleted and not address.customer.is_deleted:
            continue
        chosen.setdefault(address.customer_id, address)
    return chosen


def forwards(apps, schema_editor):
    """Move the columns into the book; on a re-apply, the columns win.

    At the 0008 state the four columns are the source of truth: after a reverse
    the old code may have edited them. So a customer whose billing address
    already exists has that row brought up to date rather than skipped, and one
    without gets a new row.

    Columns cleared while rolled back mean the address was removed. The row's
    four fields are cleared too if it still holds something else (a note, which
    only the address book has); otherwise it is soft-deleted, because an
    address with nothing in it is a row the API refuses to save and so could
    never be edited again.
    """
    customer_model = apps.get_model("customers", "Customer")
    address_model = apps.get_model("customers", "CustomerAddress")

    current = _current_billing(address_model)
    now = timezone.now()
    created, updated = [], []
    for customer in customer_model.objects.only(*CUSTOMER_COLUMNS, *ADDRESS_FIELDS).iterator():
        address = current.get(customer.pk)
        if address is None:
            if _has_address(customer):
                created.append(
                    address_model(
                        company_id=customer.company_id,
                        customer_id=customer.pk,
                        label=BILLING_LABEL,
                        is_billing=True,
                        # A soft-deleted customer's address goes with it, rather
                        # than appearing live under a record nobody can see.
                        is_deleted=customer.is_deleted,
                        deleted_at=customer.deleted_at,
                        **{field: getattr(customer, field) for field in ADDRESS_FIELDS},
                    )
                )
            continue

        values = {field: getattr(customer, field) for field in ADDRESS_FIELDS}
        if all(getattr(address, field) == value for field, value in values.items()):
            continue
        if _has_address(customer) or (address.address_note or "").strip():
            for field, value in values.items():
                setattr(address, field, value)
        elif not address.is_deleted:
            address.is_deleted = True
            address.deleted_at = now
        else:
            continue
        address.updated_at = now
        updated.append(address)

    address_model.objects.bulk_create(created, batch_size=500)
    address_model.objects.bulk_update(
        updated,
        [
            "neighborhood",
            "street",
            "building_number",
            "unit_number",
            "is_deleted",
            "deleted_at",
            "updated_at",
        ],
        batch_size=500,
    )


def backwards(apps, schema_editor):
    customer_model = apps.get_model("customers", "Customer")
    address_model = apps.get_model("customers", "CustomerAddress")

    chosen = _current_billing(address_model)
    customers = customer_model.objects.only("id", *ADDRESS_FIELDS).in_bulk(list(chosen))
    for customer_id, address in chosen.items():
        customer = customers[customer_id]
        for field in ADDRESS_FIELDS:
            setattr(customer, field, getattr(address, field))
    customer_model.objects.bulk_update(
        customers.values(),
        ["neighborhood", "street", "building_number", "unit_number"],
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0008_customer_address"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
