"""The finance summary: what the firm is owed, what it owes, and where its money is.

Read-only, and computed from the movements like every other balance
(apps.ledger.balances). Called with the firm in context: every query goes
through the tenant manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.ledger.balances import ZERO, with_balance, with_cash_balance
from apps.ledger.models import Account, CashAccount, CashDirection, Movement, MovementSource

_MONEY = DecimalField(max_digits=16, decimal_places=2)

PERIODS = ("today", "yesterday", "week", "month")

#: Money that only moved inside the firm, or was there before the ledger: a
#: transfer between tills and an opening balance are neither in nor out.
_INTERNAL = (MovementSource.TRANSFER, MovementSource.OPENING)


def period_range(period: str, today: Date) -> tuple[Date, Date]:
    """The first and last day of `period`, both inclusive, ending today.

    A week starts on Monday and a month on the 1st, as the office counts them.
    """
    if period == "today":
        return today, today
    if period == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    raise ValueError(f"Unknown period: {period}")


def _sum(condition: Q, column: str = "amount") -> Coalesce:
    return Coalesce(
        Sum(column, filter=condition, output_field=_MONEY),
        Value(ZERO, output_field=_MONEY),
        output_field=_MONEY,
    )


def _money(value: Decimal | None) -> Decimal:
    return Decimal(value or ZERO).quantize(ZERO)


@dataclass(frozen=True)
class CashLine:
    cash_account: CashAccount
    balance: Decimal
    money_in: Decimal
    money_out: Decimal


@dataclass(frozen=True)
class CurrencySummary:
    """Everything in one currency: amounts in two currencies are never added."""

    currency: str
    receivables: Decimal
    payables: Decimal
    cash_accounts: list[CashLine]
    #: The cash lines, added up: the same cash accounts, so they always agree.
    money_in: Decimal
    money_out: Decimal


@dataclass(frozen=True)
class Summary:
    period: str
    date_from: Date
    date_to: Date
    currencies: list[CurrencySummary]


def balance_totals(accounts: QuerySet[Account]) -> dict[str, Decimal]:
    """What these accounts owe the firm, what the firm owes them, and the net.

    `accounts` is annotated by `with_balance`. Receivables add the positive
    balances, payables the negative ones (as a positive amount), so a customer
    in credit does not hide a customer in debt.
    """
    totals = accounts.order_by().aggregate(
        receivables=_sum(Q(balance__gt=0), "balance"),
        payables=_sum(Q(balance__lt=0), "balance"),
    )
    receivables = _money(totals["receivables"])
    payables = _money(-Decimal(totals["payables"] or ZERO))
    return {"receivables": receivables, "payables": payables, "net": receivables - payables}


def _flows(start: Date, end: Date) -> QuerySet[Movement]:
    """The cash movements that are money in or out of the firm in the window.

    Left out:
    - a transfer between cash accounts and an opening balance, and their
      reversals: money that only moved inside the firm, or was there before;
    - a movement reversed within the window, and that reversal: a document
      made and cancelled in the period contributes nothing to it. A reversal
      of something from before the window still counts, as the correction it
      is.
    """
    return (
        Movement.objects.filter(cash_account__isnull=False, date__gte=start, date__lte=end)
        .exclude(source__in=_INTERNAL)
        .exclude(reverses__source__in=_INTERNAL)
        .exclude(reversed_by__date__gte=start, reversed_by__date__lte=end)
        .exclude(reverses__date__gte=start, reverses__date__lte=end)
    )


def summary(period: str, today: Date) -> Summary:
    """Per currency: receivables and payables today, every cash account that is
    active, holds money or moved in the window, with its balance today and its
    money in and out in the window, and those lines added up."""
    start, end = period_range(period, today)

    per_cash: dict[Any, dict[str, Any]] = {
        row["cash_account"]: dict(row)
        for row in _flows(start, end)
        .order_by()
        .values("cash_account")
        .annotate(
            money_in=_sum(Q(cash_direction=CashDirection.IN)),
            money_out=_sum(Q(cash_direction=CashDirection.OUT)),
        )
    }
    moved = set(
        Movement.objects.filter(cash_account__isnull=False, date__gte=start, date__lte=end)
        .values_list("cash_account", flat=True)
        .distinct()
    )

    lines: dict[str, list[CashLine]] = {}
    for cash in with_cash_balance(CashAccount.objects.all()).order_by("name", "id"):
        balance = _money(getattr(cash, "balance", None))
        if not (cash.is_active or balance != ZERO or cash.pk in moved or cash.pk in per_cash):
            continue
        row = per_cash.get(cash.pk, {})
        lines.setdefault(cash.currency, []).append(
            CashLine(
                cash_account=cash,
                balance=balance,
                money_in=_money(row.get("money_in")),
                money_out=_money(row.get("money_out")),
            )
        )

    owed: dict[str, dict[str, Decimal]] = {}
    accounts = with_balance(Account.objects.all())
    for currency in accounts.order_by().values_list("currency", flat=True).distinct():
        owed[currency] = balance_totals(accounts.filter(currency=currency))

    currencies = []
    for currency in sorted(set(lines) | set(owed)):
        cash_lines = lines.get(currency, [])
        totals = owed.get(currency, {"receivables": ZERO, "payables": ZERO})
        currencies.append(
            CurrencySummary(
                currency=currency,
                receivables=_money(totals["receivables"]),
                payables=_money(totals["payables"]),
                cash_accounts=cash_lines,
                money_in=_money(sum((line.money_in for line in cash_lines), ZERO)),
                money_out=_money(sum((line.money_out for line in cash_lines), ZERO)),
            )
        )
    return Summary(period=period, date_from=start, date_to=end, currencies=currencies)
