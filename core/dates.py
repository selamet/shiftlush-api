"""Calendar arithmetic more than one module needs.

Contract terms (apps.contracts.services.next_term) and billing periods
(apps.billing.periods) both step by whole calendar months, and they must step
the same way: an invoice period that drifted a day from the contract term it
bills would be a reconciliation nobody can explain. So the stepping is written
once, here.
"""

from __future__ import annotations

import calendar
from datetime import date


def add_months(day: date, months: int) -> date:
    """`day` moved by whole calendar months, clamped to the end of a short month.

    Always step from a fixed origin (`add_months(start, k * n)`), never from the
    previous result: 31 January plus one month is 28 February, and 28 February
    plus one month is 28 March — chaining would lose the 31st for good.
    """
    years, month_index = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
