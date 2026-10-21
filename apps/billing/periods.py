"""Billing periods, aligned to the contract's start date.

A contract starting on 15 March is billed 15 March-14 April, 15 April-14 May
and so on, stepped by its billing period with the same month arithmetic as
contract terms (core.dates.add_months), and the last period cut at the
contract's end date.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from apps.contracts.models import BillingPeriod
from core.dates import add_months

MONTHS: dict[str, int] = {
    BillingPeriod.MONTHLY: 1,
    BillingPeriod.QUARTERLY: 3,
    BillingPeriod.SEMIANNUAL: 6,
    BillingPeriod.ANNUAL: 12,
}


@dataclass(frozen=True)
class Period:
    start: date
    #: The last day billed: the nominal end, or the contract's end if sooner.
    end: date
    #: Where the period would end if the contract ran on. Proration counts the
    #: days covered out of the days in this nominal period.
    nominal_end: date
    months: int

    @property
    def nominal_days(self) -> int:
        return (self.nominal_end - self.start).days + 1


def periods(*, start: date, end: date, billing_period: str) -> Iterator[Period]:
    """Every period of a contract running `start` to `end`, inclusive, in order.

    Each period is stepped from the contract's start rather than from the one
    before, so a contract starting on the 31st keeps billing from the 31st (or
    the month's last day) instead of drifting to the 28th after February.
    """
    months = MONTHS[billing_period]
    index = 0
    while True:
        period_start = add_months(start, index * months)
        if period_start > end:
            return
        nominal_end = add_months(start, (index + 1) * months) - timedelta(days=1)
        yield Period(
            start=period_start, end=min(nominal_end, end), nominal_end=nominal_end, months=months
        )
        index += 1


def overlap_days(first: tuple[date, date], second: tuple[date, date]) -> int:
    """How many days two inclusive date ranges share."""
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return max(0, (end - start).days + 1)
