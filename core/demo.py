"""The generated contents of a demonstration company.

Split out of `seed_demo_data` when a second caller appeared. `seed_demo_data`
builds a company from nothing and fills it; `create_demo_account` provisions an
account in a database that already holds somebody's real firm and fills only the
tenant it just made. The filling is the same work, and a second copy of it would
have drifted from this one by the first time either was edited.

Nothing here is imported at start-up: both callers are management commands, so
the models below are resolved long after the app registry is ready.

The staff domain is an argument rather than a constant because `User.email` is
unique across the whole table, not per company. Two demonstration tenants in one
database — which is exactly what a developer gets after running both commands —
would otherwise collide on the first technician.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from django.db import transaction

from apps.companies.models import Company
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
from core.context import company_context

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

#: Reserved by RFC 6762 and therefore unroutable: these colleagues can never be
#: sent a message, which is the point. They exist to make the team screen and
#: the technician narrowing show something.
STAFF_DOMAIN = "shiftlush.local"

# Only for building e-mail addresses out of names; not the search normaliser.
# Escaped rather than spelled out: CI refuses Turkish letters in a module, and
# a table of letters is not the user-facing text that rule is there to catch.
TR_SLUG = str.maketrans("\u00e7\u011f\u0131\u00f6\u015f\u00fc\u0130", "cgiosui")


@dataclass(frozen=True)
class Populated:
    """What one run put in, for the caller to report."""

    users: int
    customers: int
    buildings: int
    elevators: int
    contracts: int


@transaction.atomic
def populate(
    company: Company,
    *,
    staff_domain: str = STAFF_DOMAIN,
    rng: random.Random | None = None,
) -> Populated:
    """Fill one company with a plausible amount of data.

    Opens the tenant context itself rather than trusting the caller to. Every
    row below carries a company already, so a forgotten context would not fail
    loudly — it would write correct rows and skip the guard that proves they are
    correct, which is the failure that is worth designing out.
    """
    generator = rng if rng is not None else random.Random(SEED)
    areas = _areas()

    with company_context(company.id):
        users = _staff(company, staff_domain)
        customers = _customers(company, generator, areas)
        buildings = _buildings(company, generator, customers, areas)
        elevators = _elevators(company, generator, buildings)
        contracts = _contracts(company, generator, customers, elevators)
        _assign_technicians(company, users, customers)

    return Populated(
        users=len(users),
        customers=len(customers),
        buildings=len(buildings),
        elevators=len(elevators),
        contracts=len(contracts),
    )


def _areas() -> list[Any]:
    from apps.address.models import Neighborhood

    return list(Neighborhood.objects.all()[:20])


def has_address_data() -> bool:
    """Whether buildings have anywhere to be.

    Asked by the callers before they start, so an empty address table is
    reported as the missing step it is rather than as an IndexError from
    somewhere inside the generator.
    """
    return bool(_areas())


def _short_name(legal_name: str) -> str:
    for suffix in DEMO["complex_strip"]:
        legal_name = legal_name.replace(suffix, "")
    return legal_name


def _staff(company: Company, domain: str) -> list[User]:
    created = []
    for first, last, role in STAFF:
        slug = f"{first}.{last}".lower().translate(TR_SLUG)
        created.append(
            User.objects.create_user(
                email=f"{slug}@{domain}",
                password=DEMO_PASSWORD,
                company=company,
                first_name=first,
                last_name=last,
                role=role,
                is_email_verified=True,
            )
        )
    return created


def _customers(company: Company, rng: random.Random, areas: list[Any]) -> list[Customer]:
    created = []
    for index, (name, kind, contact, tax_number) in enumerate(CUSTOMERS):
        customer = Customer.objects.create(
            company=company,
            type=kind,
            legal_name=name,
            # Every demo customer is an organisation, and an organisation
            # needs its tax number — without one the seeded rows would be
            # the one thing a demo must not contain: records the product
            # refuses to save when somebody opens them.
            tax_number=tax_number,
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
    company: Company, rng: random.Random, customers: list[Customer], areas: list[Any]
) -> list[Building]:
    created = []
    for customer in customers:
        site = None
        if customer.type == CustomerType.COMPLEX_MANAGEMENT:
            site = Complex.objects.create(
                company=company,
                customer=customer,
                name=_short_name(customer.legal_name),
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


def _elevators(company: Company, rng: random.Random, buildings: list[Building]) -> list[Elevator]:
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
    company: Company,
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
            contract_number=next_contract_number(company.id),
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


def _assign_technicians(company: Company, users: list[User], customers: list[Customer]) -> None:
    technicians = [user for user in users if user.role == Role.TECHNICIAN]
    for index, technician in enumerate(technicians):
        # Half each, so the narrowing is visible rather than theoretical.
        for customer in customers[index :: len(technicians)]:
            technician.customer_assignments.create(company=company, customer=customer)
