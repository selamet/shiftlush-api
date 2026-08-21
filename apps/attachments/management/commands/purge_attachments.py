"""Remove the bytes of attachments that were deleted long enough ago.

Soft delete keeps the row and leaves the object in the bucket, which is correct
for the first few weeks — a file deleted by mistake on Monday is usually noticed
by Friday — and wrong forever after: a firm that deletes a thousand photographs
a year would keep paying to store every one of them.

The row is never removed, only its `storage_key`. An audit entry saying a
document was deleted is worth nothing if the record of which document went with
it.

Scheduling belongs to the deployment (a daily cron or a scheduled container).
This command is written now so that scheduling is the only thing left to decide.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.attachments.services import count_detached_objects, purge_detached_objects
from core.context import system_context


class Command(BaseCommand):
    help = "Delete stored objects for attachments soft-deleted past the retention window."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Retention window in days. Defaults to ATTACHMENT_PURGE_AFTER_DAYS.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without touching storage.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # Runs across every company, so it opens the system context explicitly
        # rather than inheriting one. There is no request here to take it from.
        with system_context():
            if options["dry_run"]:
                count = count_detached_objects(options["days"])
                self.stdout.write(f"would purge {count} object(s)")
                return
            purged = purge_detached_objects(options["days"])

        self.stdout.write(f"purged {purged} object(s)")
