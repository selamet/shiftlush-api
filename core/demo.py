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

Everything with an address goes through `ServiceArea`, which is the one thing in
here that is read out of the database rather than written down: see `_province`
for how the province is chosen and why nothing in this module names it.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Count

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
from core.text import normalize

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

#: How many districts the firm's vans cover.
#:
#: Two, because that is what a maintenance firm is. Its customers are apartment
#: blocks and offices a technician visits several of in a day, so they sit in
#: one town — and in Turkey a town of any size is split across two or three
#: districts. Widening this to the whole province would put a customer three
#: hours up a mountain road; narrowing it to one district would put every
#: customer on the same few streets. Both are equally unlike a real portfolio.
SERVICE_AREA_DISTRICTS = 2

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
    area = _service_area()

    with company_context(company.id):
        users = _staff(company, staff_domain)
        customers = _customers(company, generator, area)
        buildings = _buildings(company, generator, customers, area)
        elevators = _elevators(company, generator, buildings, area)
        contracts = _contracts(company, generator, customers, elevators)
        _assign_technicians(company, users, customers)

    return Populated(
        users=len(users),
        customers=len(customers),
        buildings=len(buildings),
        elevators=len(elevators),
        contracts=len(contracts),
    )


@dataclass(frozen=True)
class ServiceArea:
    """The patch of the country one demonstration firm works.

    Everything with an address is placed through this, so the whole generated
    portfolio lands inside it. Before it existed each row picked from the first
    twenty neighbourhoods in the table — which, ordered by name across all
    eighty-one provinces, meant a firm with customers in Balikesir, Izmir and
    Tokat at once. No elevator maintenance firm has a portfolio like that, and a
    demonstration of one is a demonstration of a business that does not exist.
    """

    province: Any
    #: Most central first: the rota below leans on the order.
    districts: list[Any]
    #: District id to its neighbourhoods, read once rather than per row.
    places: dict[int, list[Any]]

    def district_for(self, index: int) -> Any:
        """Deal customers round the districts, rather than scattering them.

        A rota rather than a random pick: it is what guarantees that several
        customers share a district, which is the half of "coherent" that a
        uniform random choice over a province would only manage by luck.
        """
        return self.districts[index % len(self.districts)]

    def somewhere_in(self, district: Any, rng: random.Random) -> Any:
        return rng.choice(self.places[district.id])


def _service_area() -> ServiceArea:
    from apps.address.models import District, Neighborhood

    province = _province()
    districts = list(
        District.objects.filter(province=province)
        .annotate(
            # Distinct postal codes is the only signal in this dataset that
            # separates a town from a mountain valley: a rural district gets one
            # code for the whole of it, while a district that is part of a city
            # gets one per quarter. Over the real data it picks Yakutiye and
            # Palandoken out of Erzurum's twenty districts, which is where the
            # city is, and Cankaya and Yenimahalle out of Ankara's.
            codes=Count("neighborhoods__postal_code", distinct=True),
            places=Count("neighborhoods", distinct=True),
        )
        .filter(places__gt=0)
        .order_by("-codes", "-places", "name")[:SERVICE_AREA_DISTRICTS]
    )

    places: dict[int, list[Any]] = defaultdict(list)
    # One query for every neighbourhood in the area, because the alternative is
    # one per building and this runs against a table of fifty thousand rows.
    for place in Neighborhood.objects.filter(district__in=districts).order_by("id"):
        places[place.district_id].append(place)

    return ServiceArea(province=province, districts=districts, places=dict(places))


class DemoProvinceMissing(RuntimeError):
    """The province the demo data is written around is not in the address table."""


def _province() -> Any:
    """The province the demonstration firm operates in: the one named in `demo.json`.

    Named there rather than chosen here, and not derived from the dataset. An
    earlier version fell back to whichever province had the most neighbourhoods
    when the named one was absent, which is how a demonstration for a firm in
    one town came to describe customers in Balikesir, Izmir and Tokat.

    A stand-in is worse than a refusal. Nothing about the generated data
    announces which province it landed in — the customer names, the dialling
    code and the district rota all read as deliberate — so a substitution is
    invisible in exactly the artefact whose job is to be looked at.

    So this refuses. `load_address_data` is the step that was missed, and saying
    so is more use than several hundred plausible rows in the wrong town.

    The province must have neighbourhoods under it, not merely a row. Loading
    one province's districts while leaving all eighty-one province rows in place
    produces exactly that shape, and it would yield an empty service area.
    """
    from apps.address.models import Province

    wanted = DEMO["home"]["province"]
    province = (
        Province.objects.annotate(places=Count("districts__neighborhoods"))
        .filter(places__gt=0, name_normalized=normalize(wanted))
        .first()
    )
    if province is None:
        raise DemoProvinceMissing(
            f"the demo data is written for {wanted} and the address tables hold no "
            f"neighbourhoods there. Run load_address_data, and check "
            f"ADDRESS_PROVINCES if the dataset is scoped."
        )
    return province


def has_address_data() -> bool:
    """Whether buildings have anywhere to be.

    Asked by the callers before they start, so an empty address table is
    reported as the missing step it is rather than as an IndexError from
    somewhere inside the generator. One neighbourhood is enough to answer it:
    every neighbourhood carries a district and every district a province, so
    a service area can always be built around one.
    """
    from apps.address.models import Neighborhood

    return Neighborhood.objects.exists()


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


def _street(rng: random.Random) -> str:
    return f"{rng.randint(1, 60)}{DEMO['street_suffix']}"


def _phone(index: int) -> str:
    """The office number of a firm's customer.

    A landline, because that is what an estate office or a municipality answers
    on, and the dialling code is always the right one: the generator refuses to
    run anywhere but the town the data is written for.
    """
    return f"{DEMO['home']['landline_prefix']}{2340000 + index * 13:07d}"


def _customers(company: Company, rng: random.Random, area: ServiceArea) -> list[Customer]:
    created = []
    for index, (name, kind, contact, tax_number) in enumerate(CUSTOMERS):
        district = area.district_for(index)
        customer = Customer.objects.create(
            company=company,
            type=kind,
            # A municipality and a chamber of commerce are named after the
            # province they belong to, so those three rows carry `{province}`
            # rather than a province. It is the last thing that would have gone
            # stale if the dataset were ever scoped somewhere else: everything
            # else about the address already follows the data, and a customer
            # called "Erzurum Belediyesi" in Trabzon would undo that on the one
            # screen a demonstration opens first. The names stay in the JSON,
            # where Turkish belongs; only the substitution is here.
            legal_name=name.format(province=area.province.name),
            # Every demo customer is an organisation, and an organisation
            # needs its tax number — without one the seeded rows would be
            # the one thing a demo must not contain: records the product
            # refuses to save when somebody opens them.
            tax_number=tax_number,
            tax_office=rng.choice(DEMO["tax_offices"]),
            phone=_phone(index),
            neighborhood=area.somewhere_in(district, rng),
            street=_street(rng),
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


def _block(company: Company, rng: random.Random, **fields: Any) -> Building:
    """One building, with the dimensions of a building in a provincial town.

    Four to fourteen floors rather than four to twenty-four: the tall end of the
    old range is a tower block, and a firm whose portfolio is full of them is
    working somewhere other than where the rest of this data says it is.

    Flats are counted off the floors rather than drawn beside them, because two
    independent draws produced twelve-storey blocks with eleven flats in them.
    """
    floors = rng.randint(4, 14)
    return Building.objects.create(
        company=company,
        address_note=rng.choice(DEMO["address_notes"]),
        floor_count=floors,
        unit_count=floors * rng.randint(2, 5),
        **fields,
    )


def _estate(
    company: Company, rng: random.Random, customer: Customer, created: list[Building]
) -> None:
    """A complex and its blocks, at one address.

    The blocks of an estate share a neighbourhood and a street and differ by
    their entrance — A, B, C at number 12 — because that is what an estate is.
    Drawing a neighbourhood per block, which is what this did before, produced
    an "estate" whose three blocks were in three different parts of town.
    """
    site = Complex.objects.create(
        company=company,
        customer=customer,
        name=_short_name(customer.legal_name),
        neighborhood=customer.neighborhood,
        street=customer.street,
        building_number=str(rng.randint(1, 40)),
    )
    for block in range(rng.randint(2, 4)):
        letter = chr(65 + block)
        created.append(
            _block(
                company,
                rng,
                customer=customer,
                complex=site,
                name=f"{letter}{DEMO['block_suffix']}",
                type=BuildingType.RESIDENTIAL,
                neighborhood=site.neighborhood,
                street=site.street,
                building_number=f"{site.building_number}/{letter}",
            )
        )


def _premises(
    company: Company,
    rng: random.Random,
    customer: Customer,
    area: ServiceArea,
    district: Any,
    created: list[Building],
) -> None:
    """The buildings of a customer who is not an estate.

    A block management company has one block, by definition. A firm has its
    office and perhaps an annexe. A public body has several buildings spread
    across the district it belongs to — and unlike an estate's blocks, those
    genuinely are in different neighbourhoods, which is the other half of not
    over-clustering.
    """
    short = _short_name(customer.legal_name)
    if customer.type == CustomerType.PUBLIC:
        kind, names = BuildingType.PUBLIC, DEMO["public_buildings"][: rng.randint(2, 4)]
    elif customer.type == CustomerType.CORPORATE:
        kind, names = BuildingType.COMMERCIAL, ["", DEMO["annex_suffix"]][: rng.randint(1, 2)]
    else:
        kind, names = BuildingType.RESIDENTIAL, [""]

    for position, suffix in enumerate(names):
        # The first is at the address the customer is registered at; the rest
        # are elsewhere in the same district.
        here = customer.neighborhood if position == 0 else area.somewhere_in(district, rng)
        created.append(
            _block(
                company,
                rng,
                customer=customer,
                complex=None,
                name=f"{short} {suffix}".strip() if suffix else short,
                type=kind,
                neighborhood=here,
                street=customer.street if position == 0 else _street(rng),
                building_number=str(rng.randint(1, 90)),
            )
        )


def _buildings(
    company: Company, rng: random.Random, customers: list[Customer], area: ServiceArea
) -> list[Building]:
    created: list[Building] = []
    for index, customer in enumerate(customers):
        if customer.type == CustomerType.COMPLEX_MANAGEMENT:
            _estate(company, rng, customer, created)
        else:
            _premises(company, rng, customer, area, area.district_for(index), created)
    return created


def _elevators(
    company: Company, rng: random.Random, buildings: list[Building], area: ServiceArea
) -> list[Elevator]:
    from apps.elevators.services import assign_qr_token

    created = []
    counter = 0
    for building in buildings:
        for _ in range(rng.randint(1, 4)):
            counter += 1
            brand, model = rng.choice(BRANDS)
            # A registration number opens with the plate code of the province
            # that issued it, so a hard-coded 34 said Istanbul on every lift no
            # matter where the building was.
            plate = f"{area.province.id:02d}"
            # Stops come from the building rather than from the dice: a lift
            # cannot serve more floors than the block has, and one that says it
            # serves twenty-four in a six-storey block is the sort of detail
            # that makes a demonstration stop being believed.
            stops = building.floor_count or rng.randint(2, 8)
            installed = date(rng.randint(2012, 2024), rng.randint(1, 12), rng.randint(1, 28))
            last = date.today() - timedelta(days=rng.randint(30, 400))
            elevator = Elevator(
                company=company,
                building=building,
                registration_number=f"{plate}-{installed.year}-{counter:06d}",
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
                stop_count=stops,
                entrance_count=stops,
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
