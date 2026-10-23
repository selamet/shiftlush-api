"""Post the invoices M7 issued before the ledger existed, and their payments.

The work is apps.ledger.migration_helpers.carry_over_invoices, which the daily
job and the demo reuse: every issued or paid draft posts its debit once, to its
customer's account in the draft's currency, and every paid draft gets a
collection into the firm's "Devir" cash account in that currency, dated when it
was paid. Idempotent, so running it again changes nothing.

This used to be 0005. It moved here, after 0007, because the carry-over makes
an account per (customer, currency), which the constraints before 0007 refuse:
a firm with one non-TRY draft would have failed the deploy. 0005 is now a no-op,
so a database that ran the old 0005 runs this one too, and changes nothing it
already posted.

It runs the live services rather than historical models, because the ledger's
rules live there and a second copy of them here would drift. That is safe for
the database this ships to, whose schema is the live one. A fresh database has
no issued drafts, and the historical check below returns before the live
models are touched, so replaying the migrations from scratch never meets a
schema the live models do not match.
"""

from django.db import migrations


def carry_over(apps, schema_editor):
    draft_model = apps.get_model("billing", "InvoiceDraft")
    if not draft_model._default_manager.filter(
        is_deleted=False, status__in=["issued", "paid"]
    ).exists():
        return

    from apps.ledger.migration_helpers import carry_over_invoices

    carry_over_invoices()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
        ("ledger", "0007_accounts_per_currency"),
    ]

    operations = [
        migrations.RunPython(
            carry_over,
            # Movements are never deleted, and the carried-over collections are
            # documents like any other: undone by cancelling them, not here.
            reverse_code=migrations.RunPython.noop,
        )
    ]
