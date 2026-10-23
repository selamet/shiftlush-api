"""Post the issued and paid invoice drafts the ledger has not posted yet.

Runs daily as a scheduled task (core/scheduling.py, apps.ledger.tasks). This
command stays for a run by hand right after a deploy, when the old container
may have issued or marked paid an invoice after the carry-over migration.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.ledger import migration_helpers


class Command(BaseCommand):
    help = "Post issued and paid invoice drafts that have no ledger posting yet."

    def handle(self, *args: Any, **options: Any) -> None:
        # Every firm's drafts: carry_over_invoices opens the system context to
        # find them and each draft's own firm to carry it.
        run = migration_helpers.carry_over_invoices()
        self.stdout.write(f"posted {run.posted}, collected {run.collected}, failed {run.failed}")
