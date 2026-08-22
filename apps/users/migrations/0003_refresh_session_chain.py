"""Give every refresh session the chain it belongs to.

Added nullable first and filled row by row, because a column added with a
callable default is evaluated once: every existing row would have received the
*same* uuid and the whole table would have collapsed into a single chain.

Chains cannot be reconstructed backwards — nothing recorded which row rotated
into which — so each existing row becomes a chain of one. The consequence is
cosmetic and self-healing: a device that refreshes once after the deploy starts
a chain that then behaves correctly, and every row predating this migration is
gone within the thirty days a refresh token lives.
"""

from uuid import uuid4

import django.utils.timezone
from django.db import migrations, models


def start_a_chain_per_existing_row(apps, schema_editor):
    session_model = apps.get_model("users", "RefreshSession")
    for pk, created_at in session_model.objects.values_list("pk", "created_at").iterator():
        session_model.objects.filter(pk=pk).update(chain_id=uuid4(), signed_in_at=created_at)


class Migration(migrations.Migration):
    dependencies = [("users", "0002_alter_user_national_id")]

    operations = [
        migrations.AddField(
            model_name="refreshsession",
            name="chain_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="refreshsession",
            name="signed_in_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(
            start_a_chain_per_existing_row, reverse_code=migrations.RunPython.noop, elidable=True
        ),
        migrations.AlterField(
            model_name="refreshsession",
            name="chain_id",
            field=models.UUIDField(default=uuid4, editable=False),
        ),
        migrations.AlterField(
            model_name="refreshsession",
            name="signed_in_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddIndex(
            model_name="refreshsession",
            index=models.Index(fields=["user", "chain_id"], name="refresh_ses_user_id_707662_idx"),
        ),
    ]
