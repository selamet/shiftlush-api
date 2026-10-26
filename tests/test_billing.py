"""Invoice drafts — M7, slice 1.

docs/superpowers/specs/2026-10-21-m7-billing-drafts-design.md. The money comes
first: periods, proration and rounding are where a billing module is wrong
without anybody noticing, so they are pinned with exact Decimal values. Then
generation (fees, faults, VAT, notifications), the lifecycle, the API, and the
two exports.
"""

from __future__ import annotations

import csv
import io
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.billing import documents, money, services
from apps.billing.models import InvoiceDraft, InvoiceLine, InvoiceLineKind, InvoiceStatus
from apps.billing.periods import overlap_days, periods
from apps.contracts.models import (
    BillingPeriod,
    Contract,
    ContractElevator,
    ContractStatus,
    PricingType,
    Scope,
)
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.faults.models import Fault, FaultChannel, FaultStatus
from apps.notifications.models import Notification, NotificationKind
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import register_company
from core import scheduling
from core.context import company_context, system_context
from core.dates import add_months
from core.error_codes import ErrorCode
from core.scheduling import LOCAL_TZ
from tests.test_faults import api_for, make_user

D = Decimal
PASSWORD = "correct-horse-battery"


class Firm:
    def __init__(self, letter: str = "") -> None:
        suffix = letter.lower()
        self.company, self.owner = register_company(
            legal_name=f"Fatura {letter} Ltd",
            display_name=f"Fatura{letter}",
            first_name="Nur",
            last_name="Owner",
            email=f"owner{suffix}@billing.example.com",
            password=PASSWORD,
        )
        self.admin = make_user(self.company, Role.ADMIN, f"admin{suffix}@billing.example.com")
        self.accountant = make_user(
            self.company, Role.ACCOUNTANT, f"acc{suffix}@billing.example.com"
        )
        self.ops = make_user(self.company, Role.OPERATIONS, f"ops{suffix}@billing.example.com")
        self.tech = make_user(self.company, Role.TECHNICIAN, f"tech{suffix}@billing.example.com")
        with company_context(self.company.id):
            self.customer = Customer.objects.create(
                company=self.company, type=CustomerType.CORPORATE, legal_name=f"Site{letter}"
            )
            self.building = Building.objects.create(
                company=self.company,
                customer=self.customer,
                name="A Blok",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
            )
        self._n = 0

    def lift(self, name: str | None = None) -> Elevator:
        self._n += 1
        with company_context(self.company.id):
            elevator = Elevator(
                company=self.company,
                building=self.building,
                name=name or f"Lift {self._n}",
                registration_number=f"R{self._n}",
            )
            assign_qr_token(elevator)
            elevator.save()
        return elevator

    def contract(
        self,
        *,
        start: date,
        end: date,
        fee: str | None = "1000.00",
        vat: str | None = "20.00",
        pricing: str = PricingType.FLAT,
        period: str = BillingPeriod.MONTHLY,
        status: str = ContractStatus.ACTIVE,
        lifts: list[tuple[Elevator, str | None, date | None, date | None]] | None = None,
    ) -> Contract:
        self._n += 1
        with company_context(self.company.id):
            contract = Contract.objects.create(
                company=self.company,
                customer=self.customer,
                contract_number=f"C-{self._n}",
                status=status,
                scope=Scope.MAINTENANCE_ONLY,
                start_date=start,
                end_date=end,
                pricing_type=pricing,
                monthly_fee=fee,
                vat_rate=vat,
                billing_period=period,
            )
            for elevator, price, added, removed in lifts or [(self.lift(), None, None, None)]:
                ContractElevator.objects.create(
                    company=self.company,
                    contract=contract,
                    elevator=elevator,
                    unit_price=price,
                    added_at=added or start,
                    removed_at=removed,
                )
        return contract

    def drafts(self) -> list[InvoiceDraft]:
        with company_context(self.company.id):
            return list(InvoiceDraft.objects.order_by("period_start", "id"))

    def lines(self, draft: InvoiceDraft) -> list[InvoiceLine]:
        with company_context(self.company.id):
            return list(InvoiceLine.objects.filter(draft=draft).order_by("position", "id"))


@pytest.fixture
def firm(db) -> Firm:
    return Firm()


def run(day: date) -> services.DraftRun:
    with system_context():
        return services.generate(today=day)


def closed_fault(
    firm: Firm,
    elevator: Elevator,
    *,
    reported: date,
    closed: date,
    billable: bool = True,
    parts: str = "Kapi kilidi x1",
) -> Fault:
    with company_context(firm.company.id):
        return Fault.objects.create(
            company=firm.company,
            elevator=elevator,
            reference=f"F{Fault.unscoped.count():05d}",
            channel=FaultChannel.STAFF,
            description="Kapi kapanmiyor",
            status=FaultStatus.CLOSED,
            reported_at=datetime(reported.year, reported.month, reported.day, 10, tzinfo=LOCAL_TZ),
            closed_at=datetime(closed.year, closed.month, closed.day, 16, tzinfo=LOCAL_TZ),
            billable=billable,
            parts_used=parts,
        )


# ---------------------------------------------------------------------------
# Periods and money
# ---------------------------------------------------------------------------


class TestPeriods:
    def test_aligned_to_the_contract_start(self):
        found = list(
            periods(
                start=date(2026, 3, 15), end=date(2027, 3, 14), billing_period=BillingPeriod.MONTHLY
            )
        )
        assert [(p.start, p.end) for p in found[:2]] == [
            (date(2026, 3, 15), date(2026, 4, 14)),
            (date(2026, 4, 15), date(2026, 5, 14)),
        ]
        assert len(found) == 12
        assert found[-1].end == date(2027, 3, 14)

    @pytest.mark.parametrize(
        ("period", "months"),
        [
            (BillingPeriod.QUARTERLY, 3),
            (BillingPeriod.SEMIANNUAL, 6),
            (BillingPeriod.ANNUAL, 12),
        ],
    )
    def test_stepped_by_the_billing_period(self, period, months):
        first = next(periods(start=date(2026, 1, 1), end=date(2028, 1, 1), billing_period=period))
        assert first.months == months
        assert first.end == add_months(date(2026, 1, 1), months) - timedelta(days=1)

    def test_the_last_period_is_cut_at_the_end_date(self):
        *_, last = periods(
            start=date(2026, 1, 10), end=date(2026, 3, 20), billing_period=BillingPeriod.MONTHLY
        )
        assert (last.start, last.end, last.nominal_end) == (
            date(2026, 3, 10),
            date(2026, 3, 20),
            date(2026, 4, 9),
        )

    def test_the_31st_does_not_drift_after_february(self):
        found = list(
            periods(start=date(2026, 1, 31), end=date(2026, 5, 30), billing_period="monthly")
        )
        assert [p.start for p in found] == [
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
            date(2026, 4, 30),
        ]
        assert found[0].end == date(2026, 2, 27)
        assert found[1].end == date(2026, 3, 30)

    def test_overlap_days_is_inclusive(self):
        assert (
            overlap_days((date(2026, 9, 15), date(2026, 10, 14)), (date(2026, 9, 25), date.max))
            == 20
        )
        assert (
            overlap_days(
                (date(2026, 9, 15), date(2026, 9, 15)), (date(2026, 9, 15), date(2026, 9, 15))
            )
            == 1
        )
        assert (
            overlap_days((date(2026, 9, 15), date(2026, 9, 20)), (date(2026, 9, 21), date.max)) == 0
        )


class TestMoney:
    def test_half_up_not_bankers_and_not_float(self):
        assert money.round_money(D("2.675")) == D("2.68")
        assert money.round_money(D("2.665")) == D("2.67")
        assert money.round_money(D("-0.005")) == D("-0.01")

    def test_prorated_quantity(self):
        assert money.prorated_quantity(1, 31, 31) == D("1.0000")
        assert money.prorated_quantity(3, 92, 92) == D("3.0000")
        assert money.prorated_quantity(1, 17, 31) == D("0.5484")
        assert money.prorated_quantity(1, 20, 30) == D("0.6667")

    @pytest.mark.parametrize(
        ("price", "months", "covered", "days", "exact", "naive"),
        [
            # 20 of 30 days at 600: two thirds is 400.00. Multiplying the shown
            # quantity (0.6667) would charge 400.02.
            ("600.00", 1, 20, 30, "400.00", "400.02"),
            # A third and two thirds of 1000.00: 333.33 and 666.67, not the
            # 333.30 and 666.70 the rounded quantities would give.
            ("1000.00", 1, 10, 30, "333.33", "333.30"),
            ("1000.00", 1, 20, 30, "666.67", "666.70"),
            # Ten days of a 29-day February.
            ("1000.00", 1, 10, 29, "344.83", "344.80"),
            # A quarter cut after 45 of its 92 days.
            ("1000.00", 3, 45, 92, "1467.39", "1467.40"),
        ],
    )
    def test_the_amount_comes_from_the_exact_fraction(
        self, price, months, covered, days, exact, naive
    ):
        assert money.prorated_amount(D(price), months, covered, days) == D(exact)
        shown = money.prorated_quantity(months, covered, days)
        # What the rejected method would have charged, to show the case bites.
        assert money.line_amount(shown, D(price)) == D(naive)

    def test_no_price_is_zero(self):
        assert money.prorated_amount(None, 1, 10, 30) == D("0.00")
        assert money.line_amount(D("1.0000"), None) == D("0.00")

    def test_total_is_subtotal_plus_vat_exactly(self):
        subtotal, vat, total = money.totals([D("100.10"), D("200.15")], D("18.00"))
        assert (subtotal, vat, total) == (D("300.25"), D("54.05"), D("354.30"))

    def test_property_over_random_lines(self):
        # Seeded, for a repeatable sweep of inputs; nothing here is a secret.
        rng = random.Random(20261021)  # noqa: S311
        for _ in range(500):
            amounts = [
                money.prorated_amount(
                    D(rng.randint(0, 999999)) / 100,
                    rng.choice([1, 3, 6, 12]),
                    rng.randint(1, 31),
                    31,
                )
                for _ in range(rng.randint(0, 6))
            ]
            rate = D(rng.choice(["0.00", "1.00", "8.00", "10.00", "18.00", "20.00"]))
            subtotal, vat, total = money.totals(amounts, rate)
            assert subtotal == sum(amounts, D("0.00"))
            assert total == subtotal + vat
            for value in (subtotal, vat, total):
                assert value == value.quantize(D("0.01"))


def test_add_months_is_shared_with_contract_terms():
    from apps.contracts.services import next_term

    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert next_term(date(2026, 3, 15), date(2027, 3, 14)) == (date(2027, 3, 15), date(2028, 3, 14))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class TestFlatFee:
    def test_a_full_month(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        result = run(date(2026, 10, 15))

        assert len(result.created) == 1
        (draft,) = firm.drafts()
        assert (draft.period_start, draft.period_end) == (date(2026, 9, 15), date(2026, 10, 14))
        assert draft.status == InvoiceStatus.DRAFT
        assert draft.customer_id == firm.customer.id
        assert (draft.vat_rate, draft.currency) == (D("20.00"), "TRY")
        (line,) = firm.lines(draft)
        assert line.kind == InvoiceLineKind.CONTRACT_FEE
        assert (line.quantity, line.unit_price, line.amount) == (
            D("1.0000"),
            D("1000.00"),
            D("1000.00"),
        )
        assert "15.09.2026" in line.description
        assert (draft.subtotal, draft.vat_amount, draft.total) == (
            D("1000.00"),
            D("200.00"),
            D("1200.00"),
        )

    def test_not_before_the_period_has_ended(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        assert run(date(2026, 10, 14)).created == []

    def test_a_second_run_drafts_nothing(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        run(date(2026, 10, 15))
        assert run(date(2026, 10, 15)).created == []
        assert run(date(2026, 10, 16)).created == []
        assert len(firm.drafts()) == 1

    def test_quarterly_bills_three_months(self, firm):
        firm.contract(start=date(2026, 7, 1), end=date(2027, 6, 30), period=BillingPeriod.QUARTERLY)
        run(date(2026, 10, 1))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        assert (line.quantity, line.amount) == (D("3.0000"), D("3000.00"))
        assert draft.total == D("3600.00")

    def test_a_period_cut_by_the_end_date_is_prorated(self, firm):
        firm.contract(start=date(2026, 8, 1), end=date(2026, 8, 20))
        run(date(2026, 8, 21))
        (draft,) = firm.drafts()
        assert draft.period_end == date(2026, 8, 20)
        (line,) = firm.lines(draft)
        # 20 of the 31 days of 1-31 August.
        assert (line.quantity, line.amount) == (D("0.6452"), D("645.16"))
        assert (draft.subtotal, draft.vat_amount, draft.total) == (
            D("645.16"),
            D("129.03"),
            D("774.19"),
        )
        assert "(20/31" in line.description

    def test_no_fee_means_a_line_waiting_for_a_price(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14), fee=None)
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        assert line.needs_price is True
        assert line.unit_price is None
        assert line.amount == D("0.00")
        assert draft.total == D("0.00")


class TestPerElevatorFee:
    def test_each_lift_for_the_days_it_was_covered(self, firm):
        full, added, removed, unpriced = (
            firm.lift("Tam"),
            firm.lift("Yeni"),
            firm.lift("Eski"),
            firm.lift("Fiyatsiz"),
        )
        firm.contract(
            start=date(2026, 9, 15),
            end=date(2027, 9, 14),
            pricing=PricingType.PER_ELEVATOR,
            fee=None,
            lifts=[
                (full, "500.00", None, None),
                (added, "600.00", date(2026, 9, 25), None),
                (removed, "400.00", None, date(2026, 9, 24)),
                (unpriced, None, None, None),
            ],
        )
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        lines = {line.elevator_id: line for line in firm.lines(draft)}
        assert all(line.kind == InvoiceLineKind.ELEVATOR_FEE for line in lines.values())
        # The period is 15 Sep - 14 Oct: 30 days.
        assert (lines[full.pk].quantity, lines[full.pk].amount) == (D("1.0000"), D("500.00"))
        # Added 25 Sep: 20 days.
        assert (lines[added.pk].quantity, lines[added.pk].amount) == (D("0.6667"), D("400.00"))
        # Removed 24 Sep, a covered day: 10 days.
        assert (lines[removed.pk].quantity, lines[removed.pk].amount) == (D("0.3333"), D("133.33"))
        assert lines[unpriced.pk].needs_price is True
        assert draft.subtotal == D("1033.33")
        assert draft.vat_amount == D("206.67")
        assert draft.total == D("1240.00")
        # The description says what was charged, without the rounded quantity.
        assert "(20/30 günü)" in lines[added.pk].description
        assert "(10/30 günü)" in lines[removed.pk].description

    def test_a_lift_removed_before_the_period_is_not_billed(self, firm):
        gone = firm.lift()
        firm.contract(
            start=date(2026, 8, 15),
            end=date(2027, 8, 14),
            pricing=PricingType.PER_ELEVATOR,
            lifts=[(gone, "500.00", None, date(2026, 9, 1)), (firm.lift(), "500.00", None, None)],
        )
        run(date(2026, 10, 15))
        latest = firm.drafts()[-1]
        assert gone.pk not in {line.elevator_id for line in firm.lines(latest)}


class TestProrationNeverDrifts:
    def test_thirds_of_a_fee_across_lifts(self, firm):
        third, two_thirds = firm.lift(), firm.lift()
        firm.contract(
            start=date(2026, 9, 15),
            end=date(2027, 9, 14),
            pricing=PricingType.PER_ELEVATOR,
            fee=None,
            lifts=[
                (third, "1000.00", date(2026, 10, 5), None),  # 10 of 30 days
                (two_thirds, "1000.00", None, date(2026, 10, 4)),  # 20 of 30 days
            ],
        )
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        amounts = {line.elevator_id: line.amount for line in firm.lines(draft)}
        assert amounts == {third.pk: D("333.33"), two_thirds.pk: D("666.67")}
        assert draft.subtotal == D("1000.00")
        assert draft.total == draft.subtotal + draft.vat_amount

    def test_a_29_day_february_cut_at_the_end_date(self, firm):
        firm.contract(start=date(2028, 2, 1), end=date(2028, 2, 10))
        run(date(2028, 2, 11))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        assert (line.covered_days, line.period_days) == (10, 29)
        assert line.amount == D("344.83")
        assert (draft.subtotal, draft.vat_amount, draft.total) == (
            D("344.83"),
            D("68.97"),
            D("413.80"),
        )

    def test_a_quarter_cut_mid_month(self, firm):
        firm.contract(start=date(2026, 7, 1), end=date(2026, 8, 14), period=BillingPeriod.QUARTERLY)
        run(date(2026, 8, 15))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        assert (line.months, line.covered_days, line.period_days) == (3, 45, 92)
        assert line.quantity == D("1.4674")
        assert line.amount == D("1467.39")
        assert "3 aylık dönemin 45/92 günü" in line.description

    def test_pricing_a_prorated_line_later_keeps_the_exact_share(self, firm):
        lift = firm.lift()
        firm.contract(
            start=date(2026, 9, 15),
            end=date(2027, 9, 14),
            pricing=PricingType.PER_ELEVATOR,
            fee=None,
            lifts=[(lift, None, date(2026, 9, 25), None)],
        )
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        assert line.needs_price is True
        response = api_for(firm.accountant).patch(
            reverse("invoice-line-detail", args=[line.pk]), {"unit_price": "600.00"}, format="json"
        )
        assert response.data["amount"] == "400.00"

    def test_a_typed_quantity_replaces_the_fraction(self, firm):
        lift = firm.lift()
        firm.contract(
            start=date(2026, 9, 15),
            end=date(2027, 9, 14),
            pricing=PricingType.PER_ELEVATOR,
            fee=None,
            lifts=[(lift, "600.00", date(2026, 9, 25), None)],
        )
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        (line,) = firm.lines(draft)
        response = api_for(firm.accountant).patch(
            reverse("invoice-line-detail", args=[line.pk]), {"quantity": "0.5"}, format="json"
        )
        assert response.data["amount"] == "300.00"
        with company_context(firm.company.id):
            line.refresh_from_db()
        assert (line.months, line.covered_days, line.period_days) == (None, None, None)


class TestTerminatedContracts:
    def terminate(self, contract: Contract, on: date) -> None:
        from apps.contracts.services import terminate

        with company_context(contract.company_id):
            terminate(contract=contract, terminated_at=on, reason="Musteri ayrildi")

    def test_terminated_mid_period_gets_a_prorated_final_draft(self, firm):
        lift = firm.lift()
        contract = firm.contract(
            start=date(2026, 9, 1),
            end=date(2027, 8, 31),
            pricing=PricingType.PER_ELEVATOR,
            fee=None,
            lifts=[(lift, "600.00", None, None)],
        )
        run(date(2026, 10, 1))
        self.terminate(contract, date(2026, 10, 10))
        result = run(date(2026, 10, 11))

        assert len(result.created) == 1
        final = result.created[0]
        assert (final.period_start, final.period_end) == (date(2026, 10, 1), date(2026, 10, 10))
        (line,) = firm.lines(final)
        # 1-10 October, the termination day included: 10 of 31 days.
        assert (line.covered_days, line.period_days) == (10, 31)
        assert line.amount == D("193.55")
        assert run(date(2026, 10, 12)).created == []

    def test_a_flat_fee_is_cut_at_the_termination_too(self, firm):
        contract = firm.contract(start=date(2026, 9, 1), end=date(2027, 8, 31))
        run(date(2026, 10, 1))
        self.terminate(contract, date(2026, 10, 10))
        (final,) = run(date(2026, 10, 11)).created
        (line,) = firm.lines(final)
        assert line.amount == D("322.58")

    def test_terminated_on_a_period_boundary_gets_no_extra_draft(self, firm):
        contract = firm.contract(start=date(2026, 9, 1), end=date(2027, 8, 31))
        run(date(2026, 10, 1))
        self.terminate(contract, date(2026, 9, 30))
        assert run(date(2026, 10, 2)).created == []
        assert [d.period_start for d in firm.drafts()] == [date(2026, 9, 1)]

    def test_a_long_terminated_contract_is_left_alone(self, firm):
        contract = firm.contract(start=date(2025, 1, 1), end=date(2026, 12, 31))
        self.terminate(contract, date(2026, 3, 10))
        assert run(date(2026, 10, 15)).created == []


class TestFaults:
    def test_a_billable_fault_closed_in_the_period_is_a_line_to_price(self, firm):
        elevator = firm.lift("Sol")
        firm.contract(
            start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
        )
        fault = closed_fault(firm, elevator, reported=date(2026, 9, 20), closed=date(2026, 10, 2))
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        fault_lines = [line for line in firm.lines(draft) if line.kind == InvoiceLineKind.FAULT]
        assert len(fault_lines) == 1
        line = fault_lines[0]
        assert line.fault_id == fault.pk
        assert line.needs_price is True
        assert fault.reference in line.description
        assert "Sol" in line.description
        assert "Kapi kilidi x1" in line.description

    @pytest.mark.parametrize(
        ("reported", "closed", "billable"),
        [
            (date(2026, 9, 20), date(2026, 10, 15), True),  # closed after the period
            (date(2026, 9, 1), date(2026, 9, 14), True),  # closed before it
            (date(2026, 9, 20), date(2026, 10, 2), False),  # not billable
        ],
    )
    def test_other_faults_are_not(self, firm, reported, closed, billable):
        elevator = firm.lift()
        firm.contract(
            start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
        )
        closed_fault(firm, elevator, reported=reported, closed=closed, billable=billable)
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        assert [line.kind for line in firm.lines(draft)] == [InvoiceLineKind.CONTRACT_FEE]

    def test_a_fault_reported_before_this_contract_covered_the_lift_is_not_its(self, firm):
        elevator = firm.lift()
        firm.contract(
            start=date(2026, 9, 15),
            end=date(2027, 9, 14),
            lifts=[(elevator, None, date(2026, 9, 25), None)],
        )
        closed_fault(firm, elevator, reported=date(2026, 9, 20), closed=date(2026, 10, 2))
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        assert InvoiceLineKind.FAULT not in {line.kind for line in firm.lines(draft)}

    def test_a_fault_is_billed_once_and_the_database_says_so(self, firm):
        elevator = firm.lift()
        firm.contract(
            start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
        )
        fault = closed_fault(firm, elevator, reported=date(2026, 9, 20), closed=date(2026, 10, 2))
        run(date(2026, 10, 15))
        (draft,) = firm.drafts()
        with company_context(firm.company.id), pytest.raises(IntegrityError), transaction.atomic():
            InvoiceLine.objects.create(
                company=firm.company,
                draft=draft,
                kind=InvoiceLineKind.FAULT,
                fault=fault,
                description="again",
                quantity=D("1"),
            )

    def test_a_cancelled_draft_gives_its_faults_back_to_the_next_run(self, firm):
        elevator = firm.lift()
        firm.contract(
            start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
        )
        fault = closed_fault(firm, elevator, reported=date(2026, 9, 20), closed=date(2026, 10, 2))
        run(date(2026, 10, 15))
        (first,) = firm.drafts()
        response = api_for(firm.accountant).post(
            reverse("invoice-draft-cancel", args=[first.pk]), {"reason": "Yeniden hazirlanacak"}
        )
        assert response.status_code == 200, response.data

        assert len(run(date(2026, 10, 16)).created) == 1
        live = [d for d in firm.drafts() if d.status == InvoiceStatus.DRAFT]
        assert len(live) == 1
        assert fault.pk in {line.fault_id for line in firm.lines(live[0])}


class TestWhichContracts:
    def test_vat_unset_means_no_draft_and_a_notice_once(self, firm):
        contract = firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14), vat=None)
        run(date(2026, 10, 15))
        run(date(2026, 10, 16))
        assert firm.drafts() == []
        told = Notification.unscoped.filter(kind=NotificationKind.BILLING_VAT_MISSING)
        assert {n.recipient_id for n in told} == {firm.owner.id, firm.admin.id, firm.accountant.id}
        assert told.count() == 3
        assert told.first().data["contract_number"] == contract.contract_number  # type: ignore[union-attr]

    def test_a_recently_ended_contract_gets_its_last_period(self, firm):
        firm.contract(start=date(2025, 10, 1), end=date(2026, 9, 30), status=ContractStatus.EXPIRED)
        run(date(2026, 10, 5))
        assert [d.period_start for d in firm.drafts()][-1] == date(2026, 9, 1)

    def test_draft_contracts_are_not_billed(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14), status=ContractStatus.DRAFT)
        assert run(date(2026, 10, 15)).created == []

    def test_old_periods_are_not_backfilled_beyond_the_lookback(self, firm):
        firm.contract(start=date(2025, 1, 1), end=date(2027, 12, 31))
        run(date(2026, 10, 15))
        starts = [d.period_start for d in firm.drafts()]
        assert starts == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]

    def test_runs_across_firms(self, firm):
        other = Firm("B")
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        other.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        assert len(run(date(2026, 10, 15)).created) == 2
        assert {d.company_id for d in other.drafts()} == {other.company.id}


class TestReadyNotice:
    def test_one_per_firm_per_run_with_the_counts(self, firm):
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14), fee=None)
        run(date(2026, 10, 15))
        told = Notification.unscoped.filter(kind=NotificationKind.INVOICE_DRAFTS_READY)
        assert {n.recipient_id for n in told} == {firm.owner.id, firm.admin.id, firm.accountant.id}
        note = told.filter(recipient=firm.accountant).get()
        assert note.data == {"count": 2, "needs_price": 1, "period": "2026-10"}
        assert note.subject_type == "billing"
        run(date(2026, 10, 15))
        assert told.count() == 3

    def test_none_when_nothing_was_drafted(self, firm):
        run(date(2026, 10, 15))
        assert not Notification.unscoped.filter(kind=NotificationKind.INVOICE_DRAFTS_READY).exists()

    def test_it_is_scheduled_at_five(self):
        (job,) = [s for s in scheduling.get_schedules() if s.name == "draft_invoices"]
        assert (job.at.hour, job.at.minute) == (5, 0)


# ---------------------------------------------------------------------------
# Lifecycle, through the API
# ---------------------------------------------------------------------------


@pytest.fixture
def drafted(firm) -> tuple[Firm, InvoiceDraft]:
    elevator = firm.lift("Sol")
    firm.contract(
        start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
    )
    closed_fault(firm, elevator, reported=date(2026, 9, 20), closed=date(2026, 10, 2))
    run(date(2026, 10, 15))
    (draft,) = firm.drafts()
    return firm, draft


def detail(firm: Firm, draft: InvoiceDraft, user: User | None = None) -> dict[str, Any]:
    return dict(
        api_for(user or firm.accountant).get(reverse("invoice-draft-detail", args=[draft.pk])).data
    )


def fault_line(firm: Firm, draft: InvoiceDraft) -> InvoiceLine:
    return next(line for line in firm.lines(draft) if line.kind == InvoiceLineKind.FAULT)


def issue(firm: Firm, draft: InvoiceDraft, number: str = "GIB2026000000001") -> Any:
    return api_for(firm.accountant).post(
        reverse("invoice-draft-issue", args=[draft.pk]),
        {"invoice_number": number, "invoice_date": "2026-10-20"},
    )


def price(firm: Firm, draft: InvoiceDraft, value: str = "750.50") -> Any:
    return api_for(firm.accountant).patch(
        reverse("invoice-line-detail", args=[fault_line(firm, draft).pk]),
        {"unit_price": value},
        format="json",
    )


class TestLines:
    def test_pricing_a_fault_line_settles_it_and_recomputes(self, drafted):
        firm, draft = drafted
        response = price(firm, draft)
        assert response.status_code == 200, response.data
        assert response.data["needs_price"] is False
        assert response.data["amount"] == "750.50"
        body = detail(firm, draft)
        assert body["needs_price"] is False
        assert (body["subtotal"], body["vat_amount"], body["total"]) == (
            "1750.50",
            "350.10",
            "2100.60",
        )

    def test_a_manual_line(self, drafted):
        firm, draft = drafted
        response = api_for(firm.accountant).post(
            reverse("invoice-draft-lines", args=[draft.pk]),
            {"description": "Yol masrafi", "quantity": "2", "unit_price": "125.125"},
            format="json",
        )
        assert response.status_code == 400  # two decimal places for money
        response = api_for(firm.accountant).post(
            reverse("invoice-draft-lines", args=[draft.pk]),
            {"description": "Yol masrafi", "quantity": "2.5", "unit_price": "125.13"},
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["kind"] == InvoiceLineKind.MANUAL
        assert response.data["amount"] == "312.83"  # 312.825 half-up
        assert detail(firm, draft)["subtotal"] == "1312.83"

    def test_removing_a_fault_line_frees_the_fault(self, drafted):
        firm, draft = drafted
        line = fault_line(firm, draft)
        response = api_for(firm.accountant).delete(reverse("invoice-line-detail", args=[line.pk]))
        assert response.status_code == 204
        assert detail(firm, draft)["needs_price"] is False
        with company_context(firm.company.id):
            InvoiceLine.objects.create(
                company=firm.company,
                draft=draft,
                kind=InvoiceLineKind.FAULT,
                fault_id=line.fault_id,
                description="again",
                quantity=D("1"),
            )


class TestIssue:
    def test_refused_while_a_price_is_missing(self, drafted):
        firm, draft = drafted
        response = issue(firm, draft)
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.INVOICE_LINES_NEED_PRICE.value

    def test_needs_a_number_and_a_date(self, drafted):
        firm, draft = drafted
        price(firm, draft)
        response = api_for(firm.accountant).post(
            reverse("invoice-draft-issue", args=[draft.pk]), {}
        )
        assert response.status_code == 400

    def test_issued_is_frozen(self, drafted):
        firm, draft = drafted
        price(firm, draft)
        response = issue(firm, draft)
        assert response.status_code == 200, response.data
        assert response.data["status"] == InvoiceStatus.ISSUED
        assert response.data["invoice_number"] == "GIB2026000000001"
        frozen = (response.data["subtotal"], response.data["total"])

        line = fault_line(firm, draft)
        edit = api_for(firm.accountant).patch(
            reverse("invoice-line-detail", args=[line.pk]), {"unit_price": "1.00"}, format="json"
        )
        assert edit.status_code == 409
        assert edit.data["error"]["code"] == ErrorCode.INVOICE_TRANSITION_NOT_ALLOWED.value
        notes = api_for(firm.accountant).patch(
            reverse("invoice-draft-detail", args=[draft.pk]), {"notes": "x"}, format="json"
        )
        assert notes.status_code == 409
        added = api_for(firm.accountant).post(
            reverse("invoice-draft-lines", args=[draft.pk]),
            {"description": "x", "quantity": "1", "unit_price": "1.00"},
        )
        assert added.status_code == 409
        body = detail(firm, draft)
        assert (body["subtotal"], body["total"]) == frozen

    def test_the_number_is_unique_in_the_firm(self, drafted, firm):
        _, draft = drafted
        other_contract = firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        run(date(2026, 10, 15))
        second = next(d for d in firm.drafts() if d.contract_id == other_contract.pk)
        price(firm, draft)
        assert issue(firm, draft).status_code == 200
        response = issue(firm, second)
        assert response.status_code == 409
        assert response.data["error"]["code"] == ErrorCode.DUPLICATE_INVOICE_NUMBER.value
        assert issue(firm, second, number="GIB2026000000002").status_code == 200

    def test_another_firm_may_use_the_same_number(self, drafted):
        firm, draft = drafted
        other = Firm("B")
        other.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        run(date(2026, 10, 15))
        price(firm, draft)
        issue(firm, draft)
        (theirs,) = other.drafts()
        assert issue(other, theirs).status_code == 200


class TestPaidAndCancelled:
    def test_paid_only_from_issued(self, drafted):
        firm, draft = drafted
        url = reverse("invoice-draft-mark-paid", args=[draft.pk])
        early = api_for(firm.accountant).post(url, {"paid_on": "2026-10-21"})
        assert early.status_code == 409
        price(firm, draft)
        issue(firm, draft)
        assert api_for(firm.accountant).post(url, {}).status_code == 400
        paid = api_for(firm.accountant).post(url, {"paid_on": "2026-10-21"})
        assert paid.status_code == 200
        assert (paid.data["status"], paid.data["paid_on"]) == (InvoiceStatus.PAID, "2026-10-21")

    def test_cancel_needs_a_reason_and_not_from_paid(self, drafted):
        firm, draft = drafted
        url = reverse("invoice-draft-cancel", args=[draft.pk])
        assert api_for(firm.accountant).post(url, {}).status_code == 400
        price(firm, draft)
        issue(firm, draft)
        api_for(firm.accountant).post(
            reverse("invoice-draft-mark-paid", args=[draft.pk]), {"paid_on": "2026-10-21"}
        )
        refused = api_for(firm.accountant).post(url, {"reason": "x"})
        assert refused.status_code == 409
        # Paid means a collection stands behind it: that goes first.
        assert refused.data["error"]["code"] == ErrorCode.INVOICE_HAS_COLLECTIONS.value

    def test_an_issued_draft_can_be_cancelled_and_releases_its_number(self, drafted):
        firm, draft = drafted
        price(firm, draft)
        issue(firm, draft)
        cancelled = api_for(firm.accountant).post(
            reverse("invoice-draft-cancel", args=[draft.pk]), {"reason": "Hatali tutar"}
        )
        assert cancelled.status_code == 200
        assert cancelled.data["status"] == InvoiceStatus.CANCELLED
        assert all(line.fault_released for line in firm.lines(draft) if line.fault_id)


# ---------------------------------------------------------------------------
# The API around it
# ---------------------------------------------------------------------------


class TestPermissions:
    @pytest.mark.parametrize("role", ["owner", "admin", "accountant"])
    def test_billing_people_read_and_write(self, drafted, role):
        firm, draft = drafted
        client = api_for(getattr(firm, role))
        assert client.get(reverse("invoice-draft-list")).status_code == 200
        response = client.patch(
            reverse("invoice-draft-detail", args=[draft.pk]),
            {"notes": "Kontrol edildi"},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["notes"] == "Kontrol edildi"

    @pytest.mark.parametrize("role", ["ops", "tech"])
    def test_nobody_else(self, drafted, role):
        firm, draft = drafted
        client = api_for(getattr(firm, role))
        assert client.get(reverse("invoice-draft-list")).status_code == 403
        assert client.get(reverse("invoice-draft-export-csv")).status_code == 403
        assert (
            client.post(
                reverse("invoice-draft-cancel", args=[draft.pk]), {"reason": "x"}
            ).status_code
            == 403
        )

    def test_no_create_and_no_delete(self, drafted):
        firm, draft = drafted
        client = api_for(firm.accountant)
        assert client.post(reverse("invoice-draft-list"), {}).status_code == 405
        assert client.delete(reverse("invoice-draft-detail", args=[draft.pk])).status_code == 405


class TestList:
    def test_filters(self, drafted):
        firm, draft = drafted
        other_contract = firm.contract(start=date(2026, 8, 1), end=date(2027, 7, 31))
        run(date(2026, 10, 15))
        client = api_for(firm.accountant)

        def ids(**params: str) -> set[str]:
            rows = client.get(reverse("invoice-draft-list"), params).data["results"]
            return {row["id"] for row in rows}

        everything = ids()
        assert str(draft.pk) in everything
        assert ids(needs_price="true") == {str(draft.pk)}
        assert str(draft.pk) not in ids(needs_price="false")
        assert ids(contract=str(other_contract.pk)) == {
            str(d.pk) for d in firm.drafts() if d.contract_id == other_contract.pk
        }
        # 15 Sep - 14 Oct overlaps both September and October.
        assert str(draft.pk) in ids(period="2026-09")
        assert str(draft.pk) in ids(period="2026-10")
        assert str(draft.pk) not in ids(period="2026-11")
        assert ids(status="issued") == set()
        assert ids(customer=str(firm.customer.pk)) == everything
        bad = client.get(reverse("invoice-draft-list"), {"period": "2026-13"})
        assert bad.status_code == 400

    def test_the_list_does_not_query_per_row(self, firm):
        client = api_for(firm.accountant)
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        run(date(2026, 10, 15))
        with CaptureQueriesContext(connection) as one:
            client.get(reverse("invoice-draft-list"))
        for _ in range(4):
            firm.contract(
                start=date(2026, 9, 15),
                end=date(2027, 9, 14),
                pricing=PricingType.PER_ELEVATOR,
                lifts=[(firm.lift(), "100.00", None, None), (firm.lift(), "200.00", None, None)],
            )
        run(date(2026, 10, 16))
        with CaptureQueriesContext(connection) as five:
            response = client.get(reverse("invoice-draft-list"))
        assert len(response.data["results"]) == 5
        assert len(five) == len(one)

    def test_another_firms_drafts_are_out_of_reach(self, drafted):
        firm, draft = drafted
        other = Firm("B")
        assert api_for(other.accountant).get(reverse("invoice-draft-list")).data["results"] == []
        response = api_for(other.accountant).post(
            reverse("invoice-draft-cancel", args=[draft.pk]), {"reason": "x"}
        )
        assert response.status_code == 404


class TestCsv:
    def test_the_filtered_list_for_turkish_excel(self, drafted):
        firm, draft = drafted
        price(firm, draft)
        firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14), fee="99.99")
        run(date(2026, 10, 15))
        response = api_for(firm.accountant).get(
            reverse("invoice-draft-export-csv"), {"needs_price": "false"}
        )
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert "attachment" in response["Content-Disposition"]
        raw = response.content
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        rows = [row.split(";") for row in text.strip().split("\r\n")]
        assert rows[0][0] == "Müşteri"
        assert len(rows[0]) == 10
        assert len(rows) == 3  # the header and both priced drafts
        by_total = {row[6]: row for row in rows[1:]}
        assert set(by_total) == {"2100,60", "119,99"}
        priced = by_total["2100,60"]
        assert priced[2:4] == ["15.09.2026", "14.10.2026"]
        assert priced[4:6] == ["1750,50", "350,10"]
        assert priced[8] == "Taslak"

    def test_free_text_never_runs_as_a_formula(self):
        # Only attributes are read, so a stand-in draft is enough.
        draft = SimpleNamespace(
            status=InvoiceStatus.DRAFT,
            customer=SimpleNamespace(legal_name='=HYPERLINK("http://x")'),
            contract=SimpleNamespace(contract_number="+90"),
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            subtotal=Decimal("-10.50"),
            vat_amount=Decimal("-2.10"),
            total=Decimal("-12.60"),
            currency="TRY",
            invoice_number="@SUM(A1)",
        )
        text = documents.export_csv([cast(Any, draft)])
        _, row = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
        assert row[0] == '\'=HYPERLINK("http://x")'
        assert row[1] == "'+90"
        assert row[9] == "'@SUM(A1)"
        # Amounts the code formats stay numbers, negative ones included.
        assert row[4:7] == ["-10,50", "-2,10", "-12,60"]

    def test_it_is_every_matching_draft_not_a_page(self, firm):
        for _ in range(30):
            firm.contract(start=date(2026, 9, 15), end=date(2027, 9, 14))
        run(date(2026, 10, 15))
        text = (
            api_for(firm.accountant)
            .get(reverse("invoice-draft-export-csv"))
            .content.decode("utf-8-sig")
        )
        assert len(text.strip().split("\r\n")) == 31


class TestPdf:
    def test_a_draft_says_it_is_not_an_invoice(self, drafted):
        firm, draft = drafted
        with company_context(firm.company.id):
            html = documents.build_html(InvoiceDraft.objects.get(pk=draft.pk))
        assert "TASLAK — fatura değildir" in html
        assert "fiyat girilmedi" in html
        assert "1.000,00" in html

    def test_an_issued_one_does_not(self, drafted):
        firm, draft = drafted
        price(firm, draft)
        issue(firm, draft)
        with company_context(firm.company.id):
            html = documents.build_html(InvoiceDraft.objects.get(pk=draft.pk))
        assert "TASLAK" not in html
        assert "GIB2026000000001" in html
        assert "2.100,60" in html

    def test_the_endpoint_renders_or_says_the_renderer_is_missing(self, drafted):
        firm, draft = drafted
        response = api_for(firm.accountant).get(reverse("invoice-draft-pdf", args=[draft.pk]))
        try:
            import weasyprint  # noqa: F401
        except (ImportError, OSError):
            assert response.status_code == 503
            return
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")


def test_rendering_a_pdf(drafted):
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        pytest.skip("WeasyPrint or Pango is not available here")
    firm, draft = drafted
    with company_context(firm.company.id):
        pdf = documents.render_pdf(InvoiceDraft.objects.get(pk=draft.pk))
    assert pdf.startswith(b"%PDF")


def test_timezone_of_closed_at_is_istanbul(firm):
    # Closed at 23:30 Istanbul on the period's last day is inside the period,
    # though it is already the next day nowhere and 20:30 in UTC.
    elevator = firm.lift()
    firm.contract(
        start=date(2026, 9, 15), end=date(2027, 9, 14), lifts=[(elevator, None, None, None)]
    )
    with company_context(firm.company.id):
        Fault.objects.create(
            company=firm.company,
            elevator=elevator,
            reference="LATE01",
            channel=FaultChannel.STAFF,
            description="x",
            status=FaultStatus.CLOSED,
            reported_at=timezone.now().replace(year=2026, month=9, day=20),
            closed_at=datetime(2026, 10, 14, 23, 30, tzinfo=LOCAL_TZ),
            billable=True,
        )
    run(date(2026, 10, 15))
    (draft,) = firm.drafts()
    assert InvoiceLineKind.FAULT in {line.kind for line in firm.lines(draft)}
