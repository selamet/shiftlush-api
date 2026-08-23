"""Provision the account a demonstration is given, and nothing else.

A demo account made by hand from a shell exists exactly once. The next database
— a rebuild, a staging copy, a second region — has no way to get one back short
of somebody remembering what they typed, which is why this is a command and why
`DEPLOYMENT.md` names it in the procedure for bringing up an environment.

Not `seed_demo_data`, which cannot be used here for three reasons: it registers
a company of its own through `register_company` and therefore sends a real
verification e-mail; it refuses to start once any company exists, which every
live environment fails by definition; and its colleagues' addresses are fixed,
while `User.email` is unique across the whole table. This command provisions a
demonstration tenant *beside* whatever is already there.

**Why the account is an owner.** It is the role that can do the most, which is
the point of a demonstration, and the damage it can do is already bounded by
things that exist:

  - the tenant boundary stops it at its own company, so no amount of clicking
    reaches the firm's real customers, contracts or colleagues;
  - `LAST_OWNER_CANNOT_BE_DEACTIVATED` stops it from removing its own access,
    and it is the only owner of its company;
  - the colleagues it *can* deactivate are the generated ones in its own tenant.

Against that, an `admin` demo cannot open the company-settings screens at all —
`company`/WRITE is owner-only in the permission matrix — so it would hide a part
of the product from the people the account exists to show it to.

**Why the address is marked verified.** It is provisioned, not claimed: nobody
has to prove they control an address somebody else chose for them. Marking it
here rather than leaving it for a hand-edit in the admin is the difference
between a command that finishes the job and one that leaves a step to remember.
Sign-in never consults the flag; sending invitations does, and a demo that
cannot invite is a demo with a broken screen in it.

**Why the data is opt-in.** Signing in to five empty lists demonstrates nothing,
so `--with-data` exists and a demonstration environment should use it. It is not
the default because this runs against databases that hold real records, and
several hundred generated rows is a thing somebody should ask for rather than
receive as a side effect of creating a login. They land in the demo company and
nowhere else; the tenant boundary is what makes that true rather than a naming
convention.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.companies.models import Company
from apps.users.models import Role, User
from core import demo
from core.context import company_context, system_context

DEFAULT_EMAIL = "demo@selamet.dev"

# In the source and in DEPLOYMENT.md because it is meant to be known. It is
# checked against AUTH_PASSWORD_VALIDATORS below like any other, so this line
# cannot quietly fall below the policy the product enforces on its customers.
DEFAULT_PASSWORD = "demo123123"

LEGAL_NAME = "ShiftLush Demo"
DISPLAY_NAME = "ShiftLush Demo"
FIRST_NAME = "ShiftLush"
LAST_NAME = "Demo"

#: Distinct from `core.demo.STAFF_DOMAIN` so that this tenant's colleagues and
#: `seed_demo_data`'s can exist in one database. Unroutable either way.
STAFF_DOMAIN = "demo.shiftlush.local"


class Command(BaseCommand):
    help = "Create the demonstration company and its owner. Safe to run again."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", default=DEFAULT_EMAIL)
        parser.add_argument("--password", default=DEFAULT_PASSWORD)
        parser.add_argument(
            "--with-data",
            action="store_true",
            help="Also fill the demo company with generated customers, buildings, "
            "elevators and contracts. Only ever writes to the demo company.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"].strip().lower()
        company, created = self._account(email, options["password"])

        if created:
            self.stdout.write(f"created {DISPLAY_NAME} and {email} (owner, e-mail verified)")
        else:
            self.stdout.write(
                f"{email} already exists in {company.display_name} — nothing to create. "
                f"Its password is left as it is; this command never resets one."
            )

        if options["with_data"]:
            self._fill(company)

    @transaction.atomic
    def _account(self, email: str, password: str) -> tuple[Company, bool]:
        """Return the demo company, creating it and its owner if they are absent.

        The user is the anchor rather than the company, because the address is
        the thing the operator supplied and the thing a second run has to
        recognise. Two companies named "ShiftLush Demo" would be indistinguishable
        from each other; two users with one address cannot exist.
        """
        with system_context():
            existing = User.objects.filter(email=email).first()
            if existing is not None:
                if existing.company is None:
                    raise CommandError(
                        f"{email} exists but belongs to no company, so it is not a demo "
                        f"account this command can complete. Choose another address."
                    )
                return existing.company, False

            # Validated before anything is written, against the user this would
            # become: the similarity check reads the address and the name, so it
            # has to see them. A refusal here is the policy the product applies
            # to its own customers, and a demonstration account is not exempt.
            candidate = User(email=email, first_name=FIRST_NAME, last_name=LAST_NAME)
            try:
                validate_password(password, candidate)
            except ValidationError as refused:
                raise CommandError(
                    "That password does not meet AUTH_PASSWORD_VALIDATORS: "
                    + " ".join(refused.messages)
                ) from refused

            # Deliberately not `register_company`: that one sends a verification
            # message, and there is nobody to read it. The pair is still created
            # together — a company with no owner is unreachable, and an owner
            # with no company cannot be scoped to anything.
            company = Company.objects.create(legal_name=LEGAL_NAME, display_name=DISPLAY_NAME)
            User.objects.create_user(
                email=email,
                password=password,
                company=company,
                first_name=FIRST_NAME,
                last_name=LAST_NAME,
                role=Role.OWNER,
                is_email_verified=True,
            )
            return company, True

    def _fill(self, company: Company) -> None:
        """Generate the demo company's records, once.

        Both conditions below are about the same danger: `--email` pointed at a
        real account. A tenant that already has colleagues or customers is
        somebody's, not ours, and it is left alone with a message rather than
        quietly given fifty invented customers.
        """
        if not demo.has_address_data():
            raise CommandError("Run load_address_data first — buildings need somewhere to be.")

        with system_context():
            colleagues = User.objects.filter(company=company).exclude(role=Role.OWNER).exists()

        from apps.customers.models import Customer

        with company_context(company.id):
            populated = Customer.objects.exists()

        if colleagues or populated:
            self.stdout.write(
                f"{company.display_name} already holds records — left untouched. "
                f"Generated data is only ever written into an empty demo company."
            )
            return

        filled = demo.populate(company, staff_domain=STAFF_DOMAIN)
        self.stdout.write(
            f"filled {company.display_name}: users {filled.users}  "
            f"customers {filled.customers}  buildings {filled.buildings}  "
            f"elevators {filled.elevators}  contracts {filled.contracts}"
        )
