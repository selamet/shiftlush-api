"""Bring the demonstration tenant up to the current product, and nothing else.

The live demo tenant was filled when the product had only Phase 1 in it:
customers, buildings, lifts and contracts. `create_demo_account --with-data`
fills only an empty tenant, on purpose, so it cannot add the rest. This command
adds what is missing — a few months of maintenance visits, the plan, faults,
inspections, a portal account and invoice drafts, with the notifications they
raise — through the same `core.demo_activity` that fills a new demo tenant.

**Only the demo tenant.** The company is found from the demo owner's address and
must be the one `create_demo_account` provisions (legal name "ShiftLush Demo");
anything else is refused before a row is read, let alone written. Every write
then happens inside that company's tenant context.

**Idempotent.** Each step adds only what its module lacks in this tenant, and the
generators it drives are idempotent themselves; a second run reports zeros.

**No e-mail.** The run is inside `core.mail.suppressed()`: notifications appear
in the app and nothing is sent. The demo owner's address is a real mailbox.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.customers.models import Customer
from apps.users.models import User
from core import demo_activity
from core.context import company_context, system_context
from core.management.commands.create_demo_account import (
    DEFAULT_EMAIL,
    LEGAL_NAME,
    STAFF_DOMAIN,
)


class Command(BaseCommand):
    help = "Add the Phase 2 demo data the demo tenant is missing. Safe to run again."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", default=DEFAULT_EMAIL, help="The demo owner's address.")

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"].strip().lower()
        with system_context():
            owner = User.objects.select_related("company").filter(email=email).first()
        if owner is None or owner.company is None:
            raise CommandError(f"{email} is not a demo owner in this database.")
        company = owner.company
        if company.legal_name != LEGAL_NAME:
            raise CommandError(
                f"{email} belongs to {company.legal_name!r}, not the {LEGAL_NAME!r} demo "
                f"tenant. This command only ever writes to the demo tenant."
            )
        with company_context(company.id):
            if not Customer.objects.exists():
                raise CommandError(
                    f"{LEGAL_NAME} has no customers yet. Run "
                    f"`create_demo_account --with-data` first; this command only adds "
                    f"to a tenant that already holds the Phase 1 data."
                )

        added = demo_activity.populate_activity(company, staff_domain=STAFF_DOMAIN)
        self.stdout.write(
            f"{company.display_name}: visits +{added.visits}  planned visits "
            f"+{added.planned_visits}  planned days +{added.planned_days}  faults "
            f"+{added.faults}  inspections "
            f"+{added.inspections}  portal accounts +{added.portal_accounts}  drafts "
            f"+{added.drafts} (issued {added.issued})  notifications "
            f"+{added.notifications}  (no e-mail sent)"
        )
        for note in added.notes:
            self.stdout.write(f"note: {note}")
        portal = demo_activity.portal_login(company)
        if portal:
            self.stdout.write(f"portal sign in: {portal} / {demo_activity.PORTAL_PASSWORD}")
