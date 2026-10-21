"""The money arithmetic, in one place, so it is the same everywhere.

Decimal only. Two roundings, both half-up, at the two points the spec names:

- a line's `amount`, rounded to 2 places once, from the exact value: for a fee
  prorated by covered days that is `unit_price x months x covered_days /
  period_days`, computed as one fraction; for any other line, `quantity x
  unit_price`;
- the VAT is the subtotal times the rate, rounded to 2 places.

Nothing else is rounded. The subtotal is the sum of rounded line amounts and the
total is subtotal plus VAT, exactly — so the three numbers on a draft always add
up on paper, which is the first thing an accountant checks.

A prorated quantity is kept to four places for the reader, but the amount is
never computed from it: 20 of 30 days of a 600.00 fee is 400.00, not the 400.02
that 0.6667 x 600 would charge. The line's description states the fraction in
plain terms (the covered and total days, "20/30"), so the amount can be
reproduced exactly from what the draft prints.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
QUANTITY_STEP = Decimal("0.0001")
ZERO = Decimal("0.00")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def round_quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_STEP, rounding=ROUND_HALF_UP)


def prorated_quantity(months: int, covered_days: int, period_days: int) -> Decimal:
    """Months billed for `covered_days` of a `period_days`-long period."""
    if covered_days >= period_days:
        return round_quantity(Decimal(months))
    return round_quantity(Decimal(months) * Decimal(covered_days) / Decimal(period_days))


def line_amount(quantity: Decimal, unit_price: Decimal | None) -> Decimal:
    """Quantity times price, rounded; zero for a line still waiting for its price.

    For lines whose quantity is exact: a count, whole months, a manual figure.
    """
    if unit_price is None:
        return ZERO
    return round_money(quantity * unit_price)


def prorated_amount(
    unit_price: Decimal | None, months: int, covered_days: int, period_days: int
) -> Decimal:
    """`unit_price x months x covered_days / period_days`, rounded once.

    The multiplications come first and the division last, so the only inexact
    step is a single division carried at Decimal's 28 significant digits — and
    the only rounding is the final one.
    """
    if unit_price is None:
        return ZERO
    return round_money(unit_price * Decimal(months) * Decimal(covered_days) / Decimal(period_days))


def totals(amounts: Iterable[Decimal], vat_rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(subtotal, VAT, total) — and total is subtotal + VAT to the cent."""
    subtotal = round_money(sum(amounts, ZERO))
    vat = round_money(subtotal * vat_rate / Decimal(100))
    return subtotal, vat, subtotal + vat
