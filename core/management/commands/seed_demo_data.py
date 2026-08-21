"""Populate a database with a plausible company.

Exists so a developer, a designer or a demo has something realistic to look at
within a minute of cloning, and so list screens are exercised against the row
counts they will actually meet rather than against three records.

Idempotent by refusing to run twice: reseeding on top of edited data produces a
mess nobody can reason about.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.address.models import Neighborhood
from apps.contracts.models import (
    BillingPeriod,
    Contract,
    ContractElevator,
    ContractStatus,
    PricingType,
    Scope,
)
from apps.customers.models import ContactRole, Customer, CustomerContact, CustomerType
from apps.elevators.models import (
    Category,
    ControlType,
    DoorType,
    DriveType,
    Elevator,
    ElevatorStatus,
    InspectionLabel,
    MachineRoom,
)
from apps.properties.models import Building, BuildingType, Complex
from apps.users.models import Role, User
from apps.users.services import register_company
from core.context import company_context, system_context

# Deterministic, so two people looking at "the demo data" are looking at the
# same thing.
SEED = 20260822

# The names live in JSON rather than in this module. Turkish belongs in data
# files, locale catalogues and templates — never compiled into Python, which is
# what the CI check enforces.
DATA_PATH = Path(__file__).resolve().parent / "data" / "demo.json"
DEMO = json.loads(DATA_PATH.read_text(encoding="utf-8"))

CUSTOMERS = [tuple(row) for row in DEMO["customers"]]
STAFF = [tuple(row) for row in DEMO["staff"]]
BRANDS = [tuple(row) for row in DEMO["brands"]]

DEMO_PASSWORD = "shiftlush-demo-2026"

# Only for building e-mail addresses out of names; not the search normaliser.
TR_SLUG = str.maketrans("\u00e7\u011f\u0131\u00f6\u015f\u00fc\u0130", "cgiosui")


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

        rng = random.Random(SEED)
        neighborhoods = list(Neighborhood.objects.all()[:20])
        if not neighborhoods:
            raise CommandError("Run load_address_data first — buildings need somewhere to be.")

        company, owner = register_company(
            legal_name=DEMO["company"]["legal_name"],
            display_name=DEMO["company"]["display_name"],
            first_name=DEMO["company"]["first_name"],
            last_name=DEMO["company"]["last_name"],
            email=options["email"],
            password=DEMO_PASSWORD,
        )

        with company_context(company.id):
            users = self._staff(company)
            customers = self._customers(company, rng, neighborhoods)
            buildings = self._buildings(company, rng, customers, neighborhoods)
            elevators = self._elevators(company, rng, buildings)
            contracts = self._contracts(company, rng, customers, elevators)
            self._assign_technicians(company, users, customers)

        self.stdout.write(
            f"company: {company.display_name}\n"
            f"sign in: {options['email']} / {DEMO_PASSWORD}\n"
            f"users: {len(users) + 1}  customers: {len(customers)}  "
            f"buildings: {len(buildings)}  elevators: {len(elevators)}  "
            f"contracts: {len(contracts)}"
        )

    @staticmethod
    def _short_name(legal_name: str) -> str:
        for suffix in DEMO["complex_strip"]:
            legal_name = legal_name.replace(suffix, "")
        return legal_name

    def _staff(self, company: Any) -> list[User]:
        created = []
        for first, last, role in STAFF:
            slug = f"{first}.{last}".lower().translate(TR_SLUG)
            created.append(
                User.objects.create_user(
                    email=f"{slug}@shiftlush.local",
                    password=DEMO_PASSWORD,
                    company=company,
                    first_name=first,
                    last_name=last,
                    role=role,
                    is_email_verified=True,
                )
            )
        return created

    def _customers(self, company: Any, rng: random.Random, areas: list[Any]) -> list[Customer]:
        created = []
        for index, (name, kind, contact) in enumerate(CUSTOMERS):
            customer = Customer.objects.create(
                company=company,
                type=kind,
                legal_name=name,
                tax_office=rng.choice(DEMO["tax_offices"]),
                phone=f"+90216{4000000 + index:07d}",
                neighborhood=rng.choice(areas),
                street=f"{rng.randint(1, 60)}{DEMO['street_suffix']}",
                is_active=index != len(CUSTOMERS) - 1,
            )
            CustomerContact.objects.create(
                company=company,
                customer=customer,
                full_name=contact,
                role=ContactRole.MANAGER,
                phone=f"+90532{1000000 + index:07d}",
                is_primary=True,
            )
            created.append(customer)
        return created

    def _buildings(
        self, company: Any, rng: random.Random, customers: list[Customer], areas: list[Any]
    ) -> list[Building]:
        created = []
        for customer in customers:
            site = None
            if customer.type == CustomerType.COMPLEX_MANAGEMENT:
                site = Complex.objects.create(
                    company=company,
                    customer=customer,
                    name=self._short_name(customer.legal_name),
                    neighborhood=rng.choice(areas),
                )
            for block in range(rng.randint(1, 4)):
                created.append(
                    Building.objects.create(
                        company=company,
                        customer=customer,
                        complex=site,
                        name=f"{chr(65 + block)}{DEMO['block_suffix']}"
                        if site
                        else customer.legal_name,
                        type=rng.choice(
                            [BuildingType.RESIDENTIAL, BuildingType.COMMERCIAL, BuildingType.PUBLIC]
                        ),
                        neighborhood=rng.choice(areas),
                        street=f"{rng.randint(1, 60)}{DEMO['street_suffix']}",
                        building_number=str(rng.randint(1, 90)),
                        address_note=DEMO["address_note"],
                        floor_count=rng.randint(4, 24),
                        unit_count=rng.randint(8, 96),
                    )
                )
        return created

    def _elevators(
        self, company: Any, rng: random.Random, buildings: list[Building]
    ) -> list[Elevator]:
        from apps.elevators.services import assign_qr_token

        created = []
        counter = 0
        for building in buildings:
            for _ in range(rng.randint(1, 4)):
                counter += 1
                brand, model = rng.choice(BRANDS)
                installed = date(rng.randint(2012, 2024), rng.randint(1, 12), rng.randint(1, 28))
                last = date.today() - timedelta(days=rng.randint(30, 400))
                elevator = Elevator(
                    company=company,
                    building=building,
                    registration_number=f"34-{installed.year}-{counter:06d}",
                    name=rng.choice(DEMO["elevator_names"]),
                    category=rng.choice(
                        [Category.PASSENGER, Category.FREIGHT, Category.PASSENGER_FREIGHT]
                    ),
                    drive_type=rng.choice([DriveType.GEARLESS_ELECTRIC, DriveType.HYDRAULIC]),
                    control_type=ControlType.FULL_COLLECTIVE,
                    door_type=DoorType.AUTOMATIC_CENTER,
                    # Deliberately present in the data: it is a serious
                    # non-conformity and the list screen has to show it.
                    has_car_door=rng.random() > 0.15,
                    machine_room=rng.choice([MachineRoom.PRESENT, MachineRoom.ABSENT]),
                    capacity_kg=rng.choice([320, 450, 630, 1000, 1600]),
                    capacity_persons=rng.choice([4, 6, 8, 13, 21]),
                    stop_count=rng.randint(2, 24),
                    entrance_count=rng.randint(2, 24),
                    speed_mps=rng.choice(["0.63", "1.00", "1.60"]),
                    brand=brand,
                    model=model,
                    installation_date=installed,
                    last_inspection_date=last,
                    next_inspection_date=last + timedelta(days=365),
                    inspection_label=rng.choice(
                        [
                            InspectionLabel.GREEN,
                            InspectionLabel.GREEN,
                            InspectionLabel.BLUE,
                            InspectionLabel.YELLOW,
                            InspectionLabel.RED,
                            InspectionLabel.NONE,
                        ]
                    ),
                    status=ElevatorStatus.UNCONTRACTED,
                )
                assign_qr_token(elevator)
                elevator.save()
                created.append(elevator)
        return created

    def _contracts(
        self,
        company: Any,
        rng: random.Random,
        customers: list[Customer],
        elevators: list[Elevator],
    ) -> list[Contract]:
        from apps.contracts.services import next_contract_number

        by_customer: dict[Any, list[Elevator]] = {}
        for elevator in elevators:
            by_customer.setdefault(elevator.building.customer_id, []).append(elevator)

        created = []
        for customer in customers[:8]:
            covered = by_customer.get(customer.id, [])
            if not covered:
                continue
            start = date.today() - timedelta(days=rng.randint(30, 300))
            contract = Contract.objects.create(
                company=company,
                customer=customer,
                contract_number=next_contract_number(str(company.id)),
                status=ContractStatus.ACTIVE,
                scope=rng.choice([Scope.MAINTENANCE_ONLY, Scope.MAINTENANCE_AND_REPAIR]),
                start_date=start,
                end_date=start + timedelta(days=365),
                pricing_type=PricingType.PER_ELEVATOR,
                monthly_fee=f"{rng.randint(3, 12) * 500}.00",
                vat_rate="20.00",
                billing_period=rng.choice([BillingPeriod.MONTHLY, BillingPeriod.QUARTERLY]),
                auto_renew=rng.random() > 0.4,
            )
            for elevator in covered:
                ContractElevator.objects.create(
                    company=company,
                    contract=contract,
                    elevator=elevator,
                    unit_price=f"{rng.randint(8, 16) * 100}.00",
                    added_at=start,
                )
                elevator.status = ElevatorStatus.ACTIVE
                elevator.save(update_fields=["status", "updated_at"])
            created.append(contract)
        return created

    def _assign_technicians(
        self, company: Any, users: list[User], customers: list[Customer]
    ) -> None:
        technicians = [user for user in users if user.role == Role.TECHNICIAN]
        for index, technician in enumerate(technicians):
            # Half each, so the narrowing is visible rather than theoretical.
            for customer in customers[index :: len(technicians)]:
                technician.customer_assignments.create(company=company, customer=customer)
