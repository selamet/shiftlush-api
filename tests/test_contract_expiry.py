"""What happens to a contract on the day after it ends.

M2 (docs/superpowers/specs/2026-10-20-m2-notifications-design.md). Until now an
ACTIVE contract stayed ACTIVE past its end date, kept its elevators, and
`auto_renew` was a checkbox nothing read. `expire()` is the one place that
decides: renew it, or let it end.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.contracts.services import ExpiryOutcome, expire, next_term
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator, ElevatorStatus
from apps.elevators.services import assign_qr_token
from apps.properties.models import Building, BuildingType
from apps.users.services import register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"


@pytest.fixture
def firm(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Customer"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="x",
        )
        elevator = Elevator(company=company, building=building, name="Sol", registration_number="1")
        assign_qr_token(elevator)
        elevator.status = ElevatorStatus.ACTIVE
        elevator.save()
    return company, customer, elevator


def make_contract(firm, *, start: date, end: date, auto_renew: bool, vat: str | None = "20.00"):
    company, customer, elevator = firm
    with company_context(company.id):
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number="2025-0001",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=start,
            end_date=end,
            pricing_type=PricingType.PER_ELEVATOR,
            monthly_fee="1000.00",
            vat_rate=vat,
            auto_renew=auto_renew,
        )
        ContractElevator.objects.create(
            company=company,
            contract=contract,
            elevator=elevator,
            unit_price="1250.00",
            added_at=start,
        )
    return contract


@pytest.fixture(autouse=True)
def as_the_scheduled_job():
    # The job runs across every company, under the system context, and so do
    # these: expire() reads and writes through the tenant managers.
    with system_context():
        yield


class TestNextTerm:
    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            # A calendar year follows with a calendar year, leap day or not.
            (date(2027, 1, 1), date(2027, 12, 31), (date(2028, 1, 1), date(2028, 12, 31))),
            (date(2028, 1, 1), date(2028, 12, 31), (date(2029, 1, 1), date(2029, 12, 31))),
            # Whole months from mid-month stay whole months.
            (date(2026, 3, 15), date(2027, 3, 14), (date(2027, 3, 15), date(2028, 3, 14))),
            (date(2026, 7, 1), date(2026, 12, 31), (date(2027, 1, 1), date(2027, 6, 30))),
            # A term starting on the 31st is not a whole number of months —
            # no month after January ends on the 31st it started on — so it is
            # carried as the same number of days.
            (date(2026, 1, 31), date(2026, 4, 30), (date(2026, 5, 1), date(2026, 7, 29))),
            # Not a whole number of months: the same number of days.
            (date(2026, 1, 1), date(2026, 1, 10), (date(2026, 1, 11), date(2026, 1, 20))),
        ],
    )
    def test_the_next_term_has_the_same_shape(self, start, end, expected):
        assert next_term(start, end) == expected


class TestNotYet:
    def test_a_contract_still_running_is_left_alone(self, firm):
        contract = make_contract(
            firm, start=date(2026, 1, 1), end=date(2026, 12, 31), auto_renew=False
        )
        assert expire(contract, today=date(2026, 12, 31)) is None
        contract.refresh_from_db()
        assert contract.status == ContractStatus.ACTIVE

    @pytest.mark.parametrize("status", [ContractStatus.DRAFT, ContractStatus.EXPIRED])
    def test_only_an_active_contract_expires(self, firm, status):
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=False
        )
        Contract.unscoped.filter(id=contract.id).update(status=status)
        contract.refresh_from_db()
        assert expire(contract, today=date(2026, 1, 1)) is None


class TestItEnds:
    def test_it_becomes_expired_and_lets_its_elevators_go(self, firm):
        _, _, elevator = firm
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=False
        )

        result = expire(contract, today=date(2026, 1, 1))

        assert result == ExpiryOutcome(kind="expired", contract=contract, successor=None)
        contract.refresh_from_db()
        assert contract.status == ContractStatus.EXPIRED
        line = ContractElevator.unscoped.get(contract=contract)
        assert line.removed_at == date(2026, 1, 1)
        assert Elevator.unscoped.get(id=elevator.id).status == ElevatorStatus.UNCONTRACTED

    def test_running_it_again_does_nothing(self, firm):
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=False
        )
        expire(contract, today=date(2026, 1, 1))
        contract.refresh_from_db()
        assert expire(contract, today=date(2026, 1, 2)) is None


class TestItRenewsItself:
    def test_the_successor_carries_on_active_with_the_same_terms(self, firm):
        _, _, elevator = firm
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=True
        )

        result = expire(contract, today=date(2026, 1, 1))

        assert result is not None and result.kind == "auto_renewed"
        successor = result.successor
        assert successor is not None
        successor.refresh_from_db()
        contract.refresh_from_db()
        assert contract.status == ContractStatus.RENEWED
        # Active, not a draft: a draft would make every one of its elevators
        # refuse a maintenance visit until somebody noticed.
        assert successor.status == ContractStatus.ACTIVE
        assert (successor.start_date, successor.end_date) == (date(2026, 1, 1), date(2026, 12, 31))
        assert successor.vat_rate == Decimal("20.00")
        assert successor.auto_renew is True
        line = ContractElevator.unscoped.get(contract=successor, removed_at__isnull=True)
        assert line.elevator_id == elevator.id
        assert line.unit_price == Decimal("1250.00")
        assert Elevator.unscoped.get(id=elevator.id).status == ElevatorStatus.ACTIVE

    def test_without_a_vat_rate_the_successor_waits_as_a_draft(self, firm):
        # Activating it would bill an invented rate — the same refusal the
        # contract total makes (SL-85).
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=True, vat=None
        )

        result = expire(contract, today=date(2026, 1, 1))

        assert result is not None and result.kind == "auto_renewal_blocked"
        assert result.successor is not None
        result.successor.refresh_from_db()
        assert result.successor.status == ContractStatus.DRAFT

    def test_a_contract_found_late_renews_from_where_it_ended_not_from_today(self, firm):
        # The scheduler was down for a week. The new term still starts the day
        # after the old one ended — coverage has no gap and no overlap.
        contract = make_contract(
            firm, start=date(2025, 1, 1), end=date(2025, 12, 31), auto_renew=True
        )
        result = expire(contract, today=date(2026, 1, 8))
        assert result is not None and result.successor is not None
        assert result.successor.start_date == date(2026, 1, 1)
