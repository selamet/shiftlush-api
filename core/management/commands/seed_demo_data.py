"""Populate a database with a plausible company.

Exists so a developer, a designer or a demo has something realistic to look at
within a minute of cloning, and so list screens are exercised against the row
counts they will actually meet rather than against three records.

Idempotent by refusing to run twice: reseeding on top of edited data produces a
mess nobody can reason about.

For a database that already holds somebody's real firm — production, in other
words — this is the wrong command: it registers a company of its own and refuses
to start once one exists. `create_demo_account` is the one that provisions a
demonstration tenant beside a real one. Both fill it from `core.demo`.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.users.services import register_company
from core import demo
from core.context import system_context


class Command(BaseCommand):
    help = "Create one company with a realistic amount of data."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", default="demo@shiftlush.local")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow seeding even though a company already exists.",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        from apps.companies.models import Company

        with system_context():
            if Company.objects.exists() and not options["force"]:
                raise CommandError(
                    "This database already holds a company. Reseeding on top of "
                    "edited data produces a state nobody can reason about. Pass "
                    "--force if that is what you want."
                )

        if not demo.has_address_data():
            raise CommandError("Run load_address_data first — buildings need somewhere to be.")

        company, _owner = register_company(
            legal_name=demo.DEMO["company"]["legal_name"],
            display_name=demo.DEMO["company"]["display_name"],
            first_name=demo.DEMO["company"]["first_name"],
            last_name=demo.DEMO["company"]["last_name"],
            email=options["email"],
            password=demo.DEMO_PASSWORD,
        )

        filled = demo.populate(company)

        self.stdout.write(
            f"company: {company.display_name}\n"
            f"sign in: {options['email']} / {demo.DEMO_PASSWORD}\n"
            f"users: {filled.users + 1}  customers: {filled.customers}  "
            f"buildings: {filled.buildings}  elevators: {filled.elevators}  "
            f"contracts: {filled.contracts}"
        )
