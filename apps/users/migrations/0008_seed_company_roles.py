"""Give every existing firm its five roles, and every user and invitation one.

From here on a new firm gets its roles from the post_save receiver
(apps.users.signals). This seeds the firms that already exist, the same way,
and points each user and invitation at the seeded role its `role` column names.

Idempotent: a firm's role that already exists under its seed key is kept, and a
grant that already exists is skipped.

Backwards writes `role` back from the role's seed key, and refuses if anyone
holds a role the firm made itself: there is no column value that says so.
"""

from django.db import migrations

# The names a seeded role is created with: the English labels of `Role` as they
# were when this migration was written. Frozen here, like the models, so that a
# later rename of a label does not change what this migration did.
SEED_NAMES = {
    "owner": "Owner",
    "admin": "Admin",
    "operations": "Operations",
    "technician": "Technician",
    "accountant": "Accountant",
}
FIELD_ROLE = "technician"
OWNER = "owner"


def seed_roles(apps, schema_editor):
    # The catalogue is imported here rather than at module level, so that a
    # catalogue that later loses or renames a name only matters to this
    # function, never to loading the migration graph. Its seeds are what the
    # app's fixed table granted, which is what every existing firm had.
    from core.permission_catalogue import CATALOGUE, DELETE, READ, RECEIVE, WRITE

    company_model = apps.get_model("companies", "Company")
    role_model = apps.get_model("users", "CompanyRole")
    grant_model = apps.get_model("users", "RoleGrant")
    user_model = apps.get_model("users", "User")
    invitation_model = apps.get_model("users", "Invitation")

    for company_id in company_model.objects.values_list("pk", flat=True):
        roles = {}
        for key, name in SEED_NAMES.items():
            roles[key], _ = role_model.objects.get_or_create(
                company_id=company_id,
                system_key=key,
                is_deleted=False,
                defaults={
                    "name": name,
                    "is_owner": key == OWNER,
                    "assigned_only": key == FIELD_ROLE,
                    "field_staff": key == FIELD_ROLE,
                },
            )
        grant_model.objects.bulk_create(
            [
                grant_model(
                    role=roles[key],
                    resource=resource.key,
                    can_read=READ in held,
                    can_write=WRITE in held,
                    can_delete=DELETE in held,
                    can_receive=RECEIVE in held,
                )
                for resource in CATALOGUE.values()
                for key, held in resource.seeds.items()
                if key != OWNER and key in roles
            ],
            ignore_conflicts=True,
        )
        for key, role in roles.items():
            user_model.objects.filter(
                company_id=company_id, role=key, company_role__isnull=True
            ).update(company_role=role)
            invitation_model.objects.filter(
                company_id=company_id, role=key, company_role__isnull=True
            ).update(company_role=role)


def unseed_roles(apps, schema_editor):
    user_model = apps.get_model("users", "User")
    invitation_model = apps.get_model("users", "Invitation")
    for model in (user_model, invitation_model):
        custom = model.objects.filter(company_role__isnull=False, company_role__system_key="")
        if custom.exists():
            raise RuntimeError(
                f"{custom.count()} {model._meta.model_name} row(s) hold a role the firm "
                "made itself; the old `role` column has no value for that."
            )
        for key in SEED_NAMES:
            model.objects.filter(company_role__system_key=key).update(role=key)
    # The roles and grants themselves go with 0007's tables.


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0006_company_ledger_closed_through"),
        ("users", "0007_company_roles"),
    ]

    operations = [migrations.RunPython(seed_roles, unseed_roles)]
