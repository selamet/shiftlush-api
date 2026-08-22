"""Search by name, and one identifier per customer.

The two derived columns are filled here for rows that already exist, before the
constraints go on. Doing it the other way round would either fail on the first
duplicate fingerprint — every existing individual customer has an empty one —
or leave old rows unfindable by search until somebody happened to edit them.
"""

from django.conf import settings
from django.db import migrations, models

from core.crypto import fingerprint
from core.text import normalize


def fill_derived_columns(apps, schema_editor):
    Customer = apps.get_model("customers", "Customer")
    # The historical model keeps the real field class, so reading `national_id`
    # still decrypts. `update_fields` keeps the ciphertext from being rewritten
    # with a new nonce for no reason.
    for customer in Customer.objects.all().iterator():
        customer.legal_name_normalized = normalize(customer.legal_name)
        customer.national_id_fingerprint = fingerprint(customer.national_id or "")
        customer.save(update_fields=["legal_name_normalized", "national_id_fingerprint"])


def clear_derived_columns(apps, schema_editor):
    # Reversing drops the columns straight afterwards; there is nothing to undo.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("address", "0001_initial"),
        ("companies", "0002_initial"),
        ("customers", "0003_alter_customer_national_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="legal_name_normalized",
            field=models.CharField(default="", editable=False, max_length=200),
        ),
        migrations.AddField(
            model_name="customer",
            name="national_id_fingerprint",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.RunPython(fill_derived_columns, clear_derived_columns),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(
                fields=["company", "legal_name_normalized"], name="customer_name_norm_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_deleted", False), models.Q(("tax_number", ""), _negated=True)
                ),
                fields=("company", "tax_number"),
                name="uq_customer_tax_number_active",
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_deleted", False), models.Q(("national_id_fingerprint", ""), _negated=True)
                ),
                fields=("company", "national_id_fingerprint"),
                name="uq_customer_national_id_active",
            ),
        ),
    ]
