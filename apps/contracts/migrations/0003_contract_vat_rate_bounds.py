"""A VAT rate is a percentage, and the database now says so.

Nothing is backfilled and nothing is rewritten. The column stays nullable on
purpose: a draft whose terms have not been agreed is a real state, and
`renew(copy_terms=False)` produces one deliberately. What changed is that a
missing rate is no longer read as a rate of zero — that happens above the
database, in the serializer, so it needs no schema change.

Existing rows: every row with `vat_rate IS NULL` satisfies the constraint
through its first branch, and every row carrying a rate already fits
`decimal(5, 2)`. A rate between 100.01 and 999.99 could exist in principle —
the column always accepted it, and "2000" typed for 20% is exactly how it would
have got there. There are none, and if there were, this migration would refuse
to apply rather than quietly leave them in place. That is the correct failure:
such a row is a bill several times too large and belongs in front of a person,
not under an `UPDATE` written by whoever happened to run the deploy.
"""

from decimal import Decimal

import django.core.validators
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("attachments", "0003_initial"),
        ("companies", "0002_initial"),
        ("contracts", "0002_initial"),
        ("customers", "0004_customer_search_and_identifier_uniqueness"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="contract",
            name="vat_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0")),
                    django.core.validators.MaxValueValidator(Decimal("100")),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="contract",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("vat_rate__isnull", True),
                    models.Q(("vat_rate__gte", 0), ("vat_rate__lte", 100)),
                    _connector="OR",
                ),
                name="contract_vat_rate_within_bounds",
            ),
        ),
    ]
