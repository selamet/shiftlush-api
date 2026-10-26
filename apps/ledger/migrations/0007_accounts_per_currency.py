"""Accounts per (party, currency), and a carried-over till per (firm, currency).

Contracts, and so invoice drafts, can be in any currency, and an amount is only
ever added to one in the same currency. A customer with a TRY and an EUR
contract has two accounts. The firm's default till stays one, in TRY; the
"Devir" till that holds M7's carried-over payments is made per currency, on
demand (apps.ledger.migration_helpers).

Before the carry-over (0008), so the carry-over can make the second account.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0006_default_till_role"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="account",
            name="uq_ledger_account_customer",
        ),
        migrations.RemoveConstraint(
            model_name="account",
            name="uq_ledger_account_supplier",
        ),
        migrations.RemoveConstraint(
            model_name="account",
            name="uq_ledger_account_user",
        ),
        migrations.RemoveConstraint(
            model_name="cashaccount",
            name="uq_ledger_cash_account_role",
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                condition=models.Q(("customer__isnull", False), ("is_deleted", False)),
                fields=("customer", "currency"),
                name="uq_ledger_account_customer_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False), ("supplier__isnull", False)),
                fields=("supplier", "currency"),
                name="uq_ledger_account_supplier_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False), ("user__isnull", False)),
                fields=("user", "currency"),
                name="uq_ledger_account_user_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False), ("role", "default")),
                fields=("company",),
                name="uq_ledger_cash_account_default",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False), ("role", "carried_over")),
                fields=("company", "currency"),
                name="uq_ledger_cash_account_carried_over",
            ),
        ),
    ]
