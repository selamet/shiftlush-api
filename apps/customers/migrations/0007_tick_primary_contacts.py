"""Turn the monthly report on for each customer's primary contact.

So that a firm switching the feature on reaches somebody sensible without a
hundred clicks. Only a primary contact with an e-mail is ticked: the report
cannot go to an address that is not there. Nothing is sent until the firm's own
switch is on.
"""

from django.db import migrations


def tick_primary_contacts(apps, schema_editor):
    contact_model = apps.get_model("customers", "CustomerContact")
    contact_model.objects.filter(is_primary=True, is_deleted=False).exclude(email="").update(
        receives_monthly_report=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0006_receives_monthly_report"),
    ]

    operations = [
        migrations.RunPython(
            tick_primary_contacts,
            # Nothing to undo: the column goes in the previous step's reverse.
            reverse_code=migrations.RunPython.noop,
        )
    ]
