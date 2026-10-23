"""Mark each firm's "Merkez kasa" as its default till, so it is found by role.

0002 made one per firm by name, and until now it was looked up by that name: a
firm that renamed it would silently have been given a second. From here on it
is found by `role="default"` (services.create_default_cash_account).

Per firm, the oldest live cash account named "Merkez kasa" takes the role, and
only where the firm has no default till yet. Idempotent.
"""

from django.db import migrations, models

DEFAULT_CASH_ACCOUNT_NAME = "Merkez kasa"


def mark_default_tills(apps, schema_editor):
    cash_model = apps.get_model("ledger", "CashAccount")
    live = cash_model._default_manager.filter(is_deleted=False)
    with_default = set(live.filter(role="default").values_list("company_id", flat=True))
    candidates = live.filter(kind="cash", name=DEFAULT_CASH_ACCOUNT_NAME, role="").order_by(
        "company_id", "created_at", "id"
    )
    for till in candidates:
        if till.company_id in with_default:
            continue
        with_default.add(till.company_id)
        cash_model._default_manager.filter(pk=till.pk).update(role="default")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0005_carry_over_invoices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cashaccount",
            name="role",
            field=models.CharField(
                blank=True,
                choices=[("default", "Default till"), ("carried_over", "Carried over from M7")],
                max_length=20,
            ),
        ),
        migrations.RunPython(mark_default_tills, reverse_code=migrations.RunPython.noop),
    ]
