"""Remove idempotency keys whose replay window has closed.

The request path deletes an expired key lazily, when the same key arrives a
second time. That covers the one case that needs no cleaning: a key presented
twice. Every key presented once — which is nearly all of them, since the header
is insurance against a retry that usually never happens — is never read again
and never removed, so the table grows for the lifetime of the deployment.

Scheduling belongs to the deployment (a daily cron or a scheduled container).
This command is written now so that scheduling is the only thing left to decide.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from core.idempotency import count_expired_keys, purge_expired_keys


class Command(BaseCommand):
    help = "Delete idempotency keys past their expiry. Intended to run daily."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many keys would be deleted without deleting them.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # No `system_context` here, unlike purge_attachments: an idempotency key
        # is infrastructure rather than a business record, so it carries no
        # tenant manager and no soft delete for a context to switch off. The
        # expiry on the row is the only thing that selects it.
        if options["dry_run"]:
            self.stdout.write(f"would purge {count_expired_keys()} key(s)")
            return

        self.stdout.write(f"purged {purge_expired_keys()} key(s)")
