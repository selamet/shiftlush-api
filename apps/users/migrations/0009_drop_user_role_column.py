"""The old `role` columns go; `company_role` is the role from here on.

Their check constraints went in 0007, before the data migration, so that going
backwards re-adds each column empty, 0008 fills it, and only then 0007 puts the
constraint back.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0008_seed_company_roles"),
    ]

    operations = [
        # Only so that going backwards can re-add a required column to a table
        # with rows in it; 0008's reverse then writes the real values.
        migrations.AlterField(
            model_name="invitation",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "Owner"),
                    ("admin", "Admin"),
                    ("operations", "Operations"),
                    ("technician", "Technician"),
                    ("accountant", "Accountant"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.RemoveField(model_name="user", name="role"),
        migrations.RemoveField(model_name="invitation", name="role"),
        migrations.AlterField(
            model_name="invitation",
            name="company_role",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invitations",
                to="users.companyrole",
            ),
        ),
    ]
