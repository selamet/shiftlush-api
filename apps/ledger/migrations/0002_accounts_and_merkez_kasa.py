"""Open the books for the firms and customers that predate the ledger.

From here on, a customer gets its account from the post_save receiver
(apps.ledger.signals) and a firm its till from `register_company`. This gives
the same to every row that already exists: an `Account` for each live customer,
named as the customer is, and a "Merkez kasa" cash account for each firm.

Idempotent: a customer that already has a live account and a firm that already
has its till are skipped, so running it again changes nothing.
"""

from django.db import migrations

DEFAULT_CASH_ACCOUNT_NAME = "Merkez kasa"


def open_the_books(apps, schema_editor):
    company_model = apps.get_model("companies", "Company")
    customer_model = apps.get_model("customers", "Customer")
    account_model = apps.get_model("ledger", "Account")
    cash_model = apps.get_model("ledger", "CashAccount")

    with_account = set(
        account_model.objects.filter(is_deleted=False, customer__isnull=False).values_list(
            "customer_id", flat=True
        )
    )
    customers = customer_model.objects.filter(is_deleted=False).exclude(pk__in=with_account)
    for customer_id, company_id, legal_name in customers.values_list(
        "pk", "company_id", "legal_name"
    ).iterator():
        account_model.objects.create(
            company_id=company_id,
            kind="customer",
            customer_id=customer_id,
            name=legal_name,
        )

    with_till = set(
        cash_model.objects.filter(
            is_deleted=False, kind="cash", name=DEFAULT_CASH_ACCOUNT_NAME
        ).values_list("company_id", flat=True)
    )
    for company_id in (
        company_model.objects.filter(is_deleted=False)
        .exclude(pk__in=with_till)
        .values_list("pk", flat=True)
        .iterator()
    ):
        cash_model.objects.create(
            company_id=company_id, kind="cash", name=DEFAULT_CASH_ACCOUNT_NAME
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            open_the_books,
            # Nothing to undo that dropping the tables would not: going back past
            # 0001 removes them, and these rows with them.
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        )
    ]
