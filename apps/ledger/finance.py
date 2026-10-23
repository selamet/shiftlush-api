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
class Summary:
    period: str
    date_from: Date
    date_to: Date
    receivables: Decimal
    payables: Decimal
    cash_accounts: list[CashLine]
    money_in: Decimal
    money_out: Decimal


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


def summary(period: str, today: Date) -> Summary:
    """Receivables, payables, each active cash account's balance today, and the
    money that came in and went out in the period."""
    start, end = period_range(period, today)
    totals = balance_totals(with_balance(Account.objects.all()))

    flows = (
        Movement.objects.filter(cash_account__isnull=False, date__gte=start, date__lte=end)
        .exclude(source__in=_INTERNAL)
        .exclude(reverses__source__in=_INTERNAL)
    )
    per_cash: dict[Any, dict[str, Any]] = {
        row["cash_account"]: dict(row)
        for row in flows.order_by()
        .values("cash_account")
        .annotate(
            money_in=_sum(Q(cash_direction=CashDirection.IN)),
            money_out=_sum(Q(cash_direction=CashDirection.OUT)),
        )
    }

    lines = []
    for cash in with_cash_balance(CashAccount.objects.filter(is_active=True)).order_by(
        "name", "id"
    ):
        row = per_cash.get(cash.pk, {})
        lines.append(
            CashLine(
                cash_account=cash,
                balance=_money(getattr(cash, "balance", None)),
                money_in=_money(row.get("money_in")),
                money_out=_money(row.get("money_out")),
            )
        )
    overall = flows.aggregate(
        money_in=_sum(Q(cash_direction=CashDirection.IN)),
        money_out=_sum(Q(cash_direction=CashDirection.OUT)),
    )
    return Summary(
        period=period,
        date_from=start,
        date_to=end,
        receivables=totals["receivables"],
        payables=totals["payables"],
        cash_accounts=lines,
        money_in=_money(overall["money_in"]),
        money_out=_money(overall["money_out"]),
    )
