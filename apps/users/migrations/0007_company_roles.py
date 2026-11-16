"""Company roles and their grants, as rows.

The tables, and a nullable `company_role` on users and invitations beside the
old `role` column. 0008 fills them; 0009 drops the column. The column's check
constraints go here rather than in 0009 so that the three reverse cleanly (see
0009).
"""

import django.db.models.deletion
import uuid_utils.compat
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0006_company_ledger_closed_through"),
        ("users", "0006_remove_onetimetoken_one_time_token_purpose_valid_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="user", name="user_role_valid"),
        migrations.RemoveConstraint(model_name="invitation", name="invitation_role_valid"),
        migrations.CreateModel(
            name="CompanyRole",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid_utils.compat.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(db_index=True, default=False)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("name", models.CharField(max_length=60)),
                ("description", models.TextField(blank=True)),
                ("system_key", models.CharField(blank=True, max_length=20)),
                ("is_owner", models.BooleanField(default=False)),
                ("assigned_only", models.BooleanField(default=False)),
                ("field_staff", models.BooleanField(default=False)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)ss",
                        to="companies.company",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "company_role",
                "ordering": ["name", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("is_deleted", False)),
                        fields=("company", "name"),
                        name="uq_company_role_name_active",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("is_owner", True)),
                        fields=("company",),
                        name="uq_company_role_one_owner",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            models.Q(("system_key", ""), _negated=True), ("is_deleted", False)
                        ),
                        fields=("company", "system_key"),
                        name="uq_company_role_system_key_active",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("assigned_only", False), ("field_staff", True), _connector="OR"
                        ),
                        name="company_role_assigned_only_is_field_staff",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RoleGrant",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("resource", models.CharField(max_length=60)),
                ("can_read", models.BooleanField(default=False)),
                ("can_write", models.BooleanField(default=False)),
                ("can_delete", models.BooleanField(default=False)),
                ("can_receive", models.BooleanField(default=False)),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="grants",
                        to="users.companyrole",
                    ),
                ),
            ],
            options={
                "db_table": "role_grant",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("role", "resource"), name="uq_role_grant_resource"
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="user",
            name="company_role",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="users",
                to="users.companyrole",
            ),
        ),
        # Nullable until 0008 has filled it; 0009 makes it required.
        migrations.AddField(
            model_name="invitation",
            name="company_role",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invitations",
                to="users.companyrole",
            ),
        ),
    ]
