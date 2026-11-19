"""Move each customer's own address into its address book.

Until now a customer carried one address in four columns, which the web app
always presented as the billing address. Each customer with any of the four set
gets one `CustomerAddress` holding them, labelled and flagged as the billing
address. The next migration drops the columns.

Historical models carry none of the project's managers — `TenantManager` is not
`use_in_migrations` — so `objects` here is Django's plain manager: every company,
soft-deleted rows included, which is what has to move. `bulk_create` and
`update` send no signals, so the audit trail is not written for a schema move.
"""

from django.db import migrations

ADDRESS_FIELDS = ("neighborhood_id", "street", "building_number", "unit_number")
BILLING_LABEL = "Fatura adresi"


def _has_address(customer):
    return customer.neighborhood_id is not None or any(
        (getattr(customer, field) or "").strip()
        for field in ("street", "building_number", "unit_number")
    )


def forwards(apps, schema_editor):
    customer_model = apps.get_model("customers", "Customer")
    address_model = apps.get_model("customers", "CustomerAddress")

    # A customer that already holds a billing address is left alone: after a
    # reverse and a re-apply that address is the one being moved, and a second
    # would duplicate it (or, live, break the one-billing-address constraint).
    already = set(
        address_model.objects.filter(is_billing=True).values_list("customer_id", flat=True)
    )
    addresses = [
        address_model(
            company_id=customer.company_id,
            customer_id=customer.pk,
            label=BILLING_LABEL,
            is_billing=True,
            # A soft-deleted customer's address goes with it, rather than
            # appearing live under a record nobody can see.
            is_deleted=customer.is_deleted,
            deleted_at=customer.deleted_at,
            neighborhood_id=customer.neighborhood_id,
            street=customer.street,
            building_number=customer.building_number,
            unit_number=customer.unit_number,
        )
        for customer in customer_model.objects.all().iterator()
        if customer.pk not in already and _has_address(customer)
    ]
    address_model.objects.bulk_create(addresses, batch_size=500)


def backwards(apps, schema_editor):
    customer_model = apps.get_model("customers", "Customer")
    address_model = apps.get_model("customers", "CustomerAddress")

    # The live billing address, and for a soft-deleted customer the one that
    # was deleted with it, so the reverse loses nothing the forward moved. Live
    # rows sort first and win.
    chosen = {}
    billing = (
        address_model.objects.filter(is_billing=True)
        .select_related("customer")
        .order_by("customer_id", "is_deleted", "-id")
    )
    for address in billing:
        if address.is_deleted and not address.customer.is_deleted:
            continue
        chosen.setdefault(address.customer_id, address)

    customers = customer_model.objects.in_bulk(list(chosen))
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
