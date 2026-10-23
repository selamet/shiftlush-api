"""Moved to 0008_carry_over_invoices. Kept, as a no-op, for the graph.

The carry-over ran here, before 0007 made accounts one per (customer,
currency). A firm with a non-TRY draft then had no account the invoice could
post to, and the deploy failed on it. It runs after 0007 now; a database that
ran it here runs it again there, which is idempotent.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
        ("ledger", "0004_cash_account_role"),
    ]

    operations = []
