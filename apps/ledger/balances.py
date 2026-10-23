"""Balances, always computed from the movements and never stored.

An account's balance is the sum of its debits minus the sum of its credits, so
a positive balance means the party owes the firm. A cash account's balance is
the sum of what came in minus what went out. The per-row helpers and the
annotations share one expression each, so a list and a detail page cannot
disagree.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, F, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.expressions import CombinedExpression
from django.db.models.functions import Coalesce, Round

from apps.billing.models import InvoiceDraft
from apps.ledger.models import (
    Account,
    CashAccount,
    CashDirection,
    CollectionApplication,
    Direction,
    Movement,
)

ZERO = Decimal("0.00")
_MONEY = DecimalField(max_digits=16, decimal_places=2)


def _signed_sum(positive: Q, negative: Q) -> Round:
    # Rounded to the cent: SQLite sums decimals as floats, and Django quantizes
    # only a plain column, so 200 movements could add up to 825.809999999999.
    # A no-op on PostgreSQL, whose numeric sum is exact.
    return Round(_difference(positive, negative), 2, output_field=_MONEY)


def _difference(positive: Q, negative: Q) -> CombinedExpression:
    return Coalesce(
        Sum("amount", filter=positive, output_field=_MONEY),
        Value(ZERO, output_field=_MONEY),
        output_field=_MONEY,
    ) - Coalesce(
        Sum("amount", filter=negative, output_field=_MONEY),
        Value(ZERO, output_field=_MONEY),
        output_field=_MONEY,
    )


def _account_total() -> Round:
    return _signed_sum(Q(direction=Direction.DEBIT), Q(direction=Direction.CREDIT))


def _cash_total() -> Round:
    return _signed_sum(Q(cash_direction=CashDirection.IN), Q(cash_direction=CashDirection.OUT))


def account_balance(account: Account) -> Decimal:
    total = Movement.objects.filter(account=account).aggregate(balance=_account_total())
    return Decimal(total["balance"] or ZERO).quantize(ZERO)


def account_balance_before(account: Account, day: date) -> Decimal:
    """The balance of everything dated strictly before `day`: a statement's opening."""
    total = Movement.objects.filter(account=account, date__lt=day).aggregate(
        balance=_account_total()
    )
    return Decimal(total["balance"] or ZERO).quantize(ZERO)


def cash_balance(cash: CashAccount) -> Decimal:
    total = Movement.objects.filter(cash_account=cash).aggregate(balance=_cash_total())
    return Decimal(total["balance"] or ZERO).quantize(ZERO)


def with_balance(qs: QuerySet[Account]) -> QuerySet[Account]:
    """Annotate `balance` on each account, in the same query as the list.

    A correlated subquery rather than a join with GROUP BY, so it composes with
    whatever else the caller has filtered, ordered or annotated.
    """
    per_account = (
        Movement.objects.filter(account=OuterRef("pk"))
        .order_by()
        .values("account")
        .annotate(total=_account_total())
        .values("total")
    )
    return qs.annotate(
        balance=Coalesce(
            Subquery(per_account, output_field=_MONEY),
            Value(ZERO, output_field=_MONEY),
            output_field=_MONEY,
        )
    )


def with_cash_balance(qs: QuerySet[CashAccount]) -> QuerySet[CashAccount]:
    """Annotate `balance` on each cash account; see `with_balance`."""
    per_cash = (
        Movement.objects.filter(cash_account=OuterRef("pk"))
        .order_by()
        .values("cash_account")
        .annotate(total=_cash_total())
        .values("total")
    )
    return qs.annotate(
        balance=Coalesce(
            Subquery(per_cash, output_field=_MONEY),
            Value(ZERO, output_field=_MONEY),
            output_field=_MONEY,
        )
    )


def with_collected(qs: QuerySet[InvoiceDraft]) -> QuerySet[InvoiceDraft]:
    """Annotate `collected` and `remaining` on each invoice draft.

    The same sums as services.invoice_collected and invoice_remaining: what
    collections still standing have paid of it, and its total less that.
    """
    per_invoice = (
        CollectionApplication.objects.filter(
            invoice=OuterRef("pk"),
            collection__cancelled_at__isnull=True,
            collection__is_deleted=False,
        )
        .order_by()
        .values("invoice")
        .annotate(total=Round(Sum("amount", output_field=_MONEY), 2, output_field=_MONEY))
        .values("total")
    )
    collected = Coalesce(
        Subquery(per_invoice, output_field=_MONEY),
        Value(ZERO, output_field=_MONEY),
        output_field=_MONEY,
    )
    return qs.annotate(collected=collected).annotate(
        remaining=Round(F("total") - F("collected"), 2, output_field=_MONEY)
    )
