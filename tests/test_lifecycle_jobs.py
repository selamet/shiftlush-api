"""The three daily jobs M2 adds, and who each one tells.

docs/superpowers/specs/2026-10-20-m2-notifications-design.md. Each job is run
directly (`.call()`), with "today" pinned, and judged by the notifications it
leaves behind — the thing a person actually sees.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest

from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.contracts.services import expire
from apps.contracts.tasks import contract_expiry, contract_renewal_notice
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator, ElevatorStatus
from apps.elevators.services import assign_qr_token
from apps.maintenance.tasks import maintenance_overdue
from apps.notifications.models import Notification, NotificationKind
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import register_company
from core import scheduling
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"
TODAY = date(2026, 10, 20)


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
        other_customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Other"
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

    people: dict[str, User] = {"owner": owner}
    for role in (Role.ADMIN, Role.OPERATIONS, Role.ACCOUNTANT, Role.TECHNICIAN):
        people[role] = make_user(company, role, f"{role}@example.com")
    people["other_technician"] = make_user(company, Role.TECHNICIAN, "elsewhere@example.com")
    with company_context(company.id):
        people[Role.TECHNICIAN].customer_assignments.create(company=company, customer=customer)
        people["other_technician"].customer_assignments.create(
            company=company, customer=other_customer
        )
    return company, customer, elevator, people


def make_user(company: Any, role: str, email: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name=role.title(),
            last_name="Person",
            role=role,
        )


def contract_for(
    firm: Any,
    *,
    end: date,
    start: date | None = None,
    elevator: Elevator | None = None,
    **extra: Any,
) -> Contract:
    company, customer, default_elevator, _ = firm
    elevator = elevator or default_elevator
    fields: dict[str, Any] = {
        "status": ContractStatus.ACTIVE,
        "renewal_notice_days": 60,
        "auto_renew": False,
        "vat_rate": "20.00",
        **extra,
    }
    with company_context(company.id):
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number=f"C-{Contract.unscoped.count() + 1}",
            scope=Scope.MAINTENANCE_ONLY,
            start_date=start or end - timedelta(days=364),
            end_date=end,
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            **fields,
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=contract.start_date
        )
    return contract


def notified(kind: str) -> set[str]:
    return {
        n.recipient.email
        for n in Notification.unscoped.filter(kind=kind).select_related("recipient")
    }


@pytest.fixture
def today():
    with (
        mock.patch("apps.contracts.tasks.local_today", return_value=TODAY),
        mock.patch("apps.maintenance.tasks.local_today", return_value=TODAY),
    ):
        yield TODAY


@pytest.mark.usefixtures("today")
class TestRenewalNotice:
    def test_owners_and_admins_hear_once_the_window_opens(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=30))

        assert contract_renewal_notice.call() == 2
        assert notified(NotificationKind.CONTRACT_RENEWAL_DUE) == {
            "owner@example.com",
            "admin@example.com",
        }

    def test_once_per_term_however_many_mornings_the_window_spans(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=30))
        contract_renewal_notice.call()
        assert contract_renewal_notice.call() == 0

    def test_not_before_the_contracts_own_notice_period(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=30), renewal_notice_days=14)
        assert contract_renewal_notice.call() == 0

    def test_a_draft_is_not_warned_about(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=30), status=ContractStatus.DRAFT)
        assert contract_renewal_notice.call() == 0

    def test_the_notice_carries_what_the_sentence_needs(self, firm):
        contract = contract_for(firm, end=TODAY + timedelta(days=30), auto_renew=True)
        contract_renewal_notice.call()
        notification = Notification.unscoped.filter(recipient__role=Role.OWNER).get()
        assert notification.subject_type == "contract"
        assert notification.subject_id == contract.id
        assert notification.data == {
            "contract_number": contract.contract_number,
            "customer_name": "Customer",
            "end_date": (TODAY + timedelta(days=30)).isoformat(),
            "auto_renew": True,
        }


@pytest.mark.usefixtures("today")
class TestExpiry:
    def test_an_ended_contract_is_closed_and_owners_are_told(self, firm):
        contract = contract_for(firm, end=TODAY - timedelta(days=1))

        assert contract_expiry.call() == 1

        assert Contract.unscoped.get(id=contract.id).status == ContractStatus.EXPIRED
        assert notified(NotificationKind.CONTRACT_EXPIRED) == {
            "owner@example.com",
            "admin@example.com",
        }

    def test_an_auto_renewal_links_to_the_new_contract(self, firm):
        contract = contract_for(firm, end=TODAY - timedelta(days=1), auto_renew=True)

        contract_expiry.call()

        notification = Notification.unscoped.filter(
            kind=NotificationKind.CONTRACT_AUTO_RENEWED, recipient__role=Role.OWNER
        ).get()
        successor = Contract.unscoped.get(previous_contract=contract)
        assert notification.subject_id == successor.id
        assert notification.data["successor_contract_number"] == successor.contract_number

    def test_a_blocked_auto_renewal_says_so(self, firm):
        contract_for(firm, end=TODAY - timedelta(days=1), auto_renew=True, vat_rate=None)
        contract_expiry.call()
        assert notified(NotificationKind.CONTRACT_AUTO_RENEWAL_BLOCKED)

    def test_one_contract_that_cannot_close_does_not_stop_the_rest(self, firm):
        company, _, elevator, _ = firm
        first = contract_for(firm, end=TODAY - timedelta(days=2))
        with company_context(company.id):
            # An elevator is on one open contract at a time.
            another = Elevator(
                company=company, building=elevator.building, name="Sag", registration_number="2"
            )
            assign_qr_token(another)
            another.save()
        second = contract_for(firm, end=TODAY - timedelta(days=1), elevator=another)

        def flaky(contract: Contract, **kwargs: Any) -> Any:
            if contract.id == first.id:
                raise RuntimeError("constraint nobody foresaw")
            return expire(contract, **kwargs)

        with mock.patch("apps.contracts.tasks.expire", side_effect=flaky):
            assert contract_expiry.call() == 1

        assert Contract.unscoped.get(id=second.id).status == ContractStatus.EXPIRED

    def test_running_it_twice_tells_nobody_twice(self, firm):
        contract_for(firm, end=TODAY - timedelta(days=1))
        contract_expiry.call()
        assert contract_expiry.call() == 0
        assert Notification.unscoped.filter(kind=NotificationKind.CONTRACT_EXPIRED).count() == 2


@pytest.mark.usefixtures("today")
class TestMaintenanceOverdue:
    def overdue(self, firm: Any) -> Elevator:
        _, _, elevator, _ = firm
        Elevator.unscoped.filter(id=elevator.id).update(
            next_maintenance_date=TODAY - timedelta(days=3)
        )
        return elevator

    def test_the_office_and_the_lifts_own_technician_hear_about_it(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=200))
        self.overdue(firm)

        maintenance_overdue.call()

        assert notified(NotificationKind.MAINTENANCE_OVERDUE) == {
            "owner@example.com",
            "admin@example.com",
            "operations@example.com",
            "technician@example.com",
        }

    def test_once_per_missed_date_not_once_a_day(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=200))
        self.overdue(firm)
        maintenance_overdue.call()
        assert maintenance_overdue.call() == 0

    def test_a_lift_no_active_contract_covers_is_owed_nothing(self, firm):
        contract_for(firm, end=TODAY + timedelta(days=200), status=ContractStatus.DRAFT)
        self.overdue(firm)
        assert maintenance_overdue.call() == 0

    def test_a_lift_that_is_not_overdue_is_left_alone(self, firm):
        _, _, elevator, _ = firm
        contract_for(firm, end=TODAY + timedelta(days=200))
        Elevator.unscoped.filter(id=elevator.id).update(next_maintenance_date=TODAY)
        assert maintenance_overdue.call() == 0


class TestTheyAreScheduled:
    def test_all_three_are_on_the_daily_schedule(self):
        names = {s.name for s in scheduling.get_schedules()}
        assert {"contract_expiry", "contract_renewal_notice", "maintenance_overdue"} <= names
