"""Posting: the only code that writes movements.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. Every document that
moves money (an invoice, a collection, a payment out, a manual entry, a
transfer, an opening balance) calls `post`, and every cancellation calls
`reverse_document`. Nothing else creates a `Movement`, and nothing at all
changes or removes one: the model refuses (`MovementImmutableError`).
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Model
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.companies.models import Company
from apps.ledger.models import (
    Account,
    CashAccount,
    CashDirection,
    Direction,
    Movement,
    MovementSource,
    OpeningBalance,
)
from apps.users.models import User
from core.error_codes import ErrorCode
from core.exceptions import Conflict, field_error
from core.scheduling import local_today

_CENT = Decimal("0.01")

_MIRROR_DIRECTION = {
    Direction.DEBIT.value: Direction.CREDIT.value,
    Direction.CREDIT.value: Direction.DEBIT.value,
    "": "",
}
_MIRROR_CASH_DIRECTION = {
    CashDirection.IN.value: CashDirection.OUT.value,
    CashDirection.OUT.value: CashDirection.IN.value,
    "": "",
}


def _invalid(field: str, reason: str) -> ValidationError:
    """A plain `invalid` on one field, for rules with no code of their own."""
    return ValidationError({field: ErrorDetail(reason, code="invalid")})


def _fresh_company(company_id: UUID) -> Company:
    # Read now rather than through a relation the caller may have cached: the
    # closed-through date has to be the one in force at the moment of writing.
    return Company.objects.get(pk=company_id)


def document_type(document: Model) -> str:
    """How a movement names its source document: "app_label.model_name"."""
    meta = document._meta
    return f"{meta.app_label}.{meta.model_name}"


def check_date(company: Company, day: Date) -> None:
    """A movement's business date: not after today, and not in a closed period.

    "Closed through" is inclusive: with it set to 30 September, 30 September
    itself is frozen and 1 October is the first day open.
    """
    if day > local_today():
        raise _invalid("date", "A movement cannot be dated after today.")
    closed_through = company.ledger_closed_through
    if closed_through is not None and day <= closed_through:
        raise field_error("date", ErrorCode.PERIOD_CLOSED)


def is_money(amount: Decimal) -> bool:
    """A finite amount above zero, to the cent. NaN and infinity are not money,
    and checked first: comparing a NaN raises rather than answering."""
    return amount.is_finite() and amount > 0 and amount == amount.quantize(_CENT)


def lock_sides(*sides: Account | CashAccount | None) -> None:
    """Lock the account and cash-account rows a write is about to touch.

    Every posting takes these locks, and so does record_opening, so the
    opening-first rule is checked by one writer at a time per side. Accounts
    first, then cash accounts, each in primary-key order, so two writers
    locking the same rows (two transfers in opposite directions, say) take
    them in the same order and never wait on each other in a cycle.
    """
    for model in (Account, CashAccount):
        pks = sorted({side.pk for side in sides if isinstance(side, model)})
        if pks:
            list(model.unscoped.select_for_update().filter(pk__in=pks).order_by("pk"))


def _check_after_opening(
    *, account: Account | None, cash_account: CashAccount | None, day: Date
) -> None:
    """Nothing is dated before a live opening balance on the side it touches."""
    for field, side in (("account", account), ("cash_account", cash_account)):
        if side is None:
            continue
        later_opening = OpeningBalance.unscoped.filter(
            is_deleted=False, cancelled_at__isnull=True, date__gt=day, **{field: side}
        )
        if later_opening.exists():
            raise _invalid("date", "Earlier than the opening balance")


def post(
    *,
    account: Account | None,
    cash_account: CashAccount | None,
    direction: str | None,
    cash_direction: str | None,
    amount: Decimal,
    currency: str,
    date: Date,
    source: str,
    document: Model | None,
    description: str,
    by: User | None,
) -> Movement:
    """Write one movement, on a party's account, a cash account, or both.

    Every rule is checked before anything is written, so a refused posting
    leaves the books as they were. The sides are locked first (`lock_sides`),
    so a posting and an opening balance on the same side never pass their
    checks at the same time.
    """
    if source not in MovementSource.values or source == MovementSource.REVERSAL:
        # A reversal names the movement it cancels; reverse_document writes it.
        raise _invalid("source", "Not a source a posting can have.")
    if account is None and cash_account is None:
        raise _invalid("account", "A movement needs an account, a cash account, or both.")
    if (account is None) != (not direction) or (direction and direction not in Direction.values):
        raise _invalid("direction", "A direction is required with an account, and only then.")
    if (cash_account is None) != (not cash_direction) or (
        cash_direction and cash_direction not in CashDirection.values
    ):
        raise _invalid(
            "cash_direction", "A cash direction is required with a cash account, and only then."
        )
    if not is_money(amount):
        raise _invalid("amount", "An amount is positive, to two decimal places.")

    sides = [side for side in (account, cash_account) if side is not None]
    if len({side.company_id for side in sides}) != 1:
        raise _invalid("cash_account", "The account and the cash account belong to one firm.")
    if any(side.currency != currency for side in sides):
        raise field_error("currency", ErrorCode.CURRENCY_MISMATCH)
    check_date(_fresh_company(sides[0].company_id), date)

    with transaction.atomic():
        lock_sides(account, cash_account)
        if source != MovementSource.OPENING:
            _check_after_opening(account=account, cash_account=cash_account, day=date)
        return Movement.objects.create(
            company_id=sides[0].company_id,
            account=account,
            cash_account=cash_account,
            date=date,
            direction=direction or "",
            cash_direction=cash_direction or "",
            amount=amount,
            currency=currency,
            source=source,
            document_type=document_type(document) if document is not None else "",
            document_id=document.pk if document is not None else None,
            description=description,
            created_by=by,
            updated_by=by,
        )


def reverse_document(
    document: Model,
    *,
    date: Date,
    by: User | None,
    source: str = MovementSource.REVERSAL,
) -> list[Movement]:
    """Cancel a document: post the mirror of each of its unreversed movements.

    A mirror keeps the amount, currency, sides and document and flips both
    directions, so every balance the document touched returns to where it was.
    A document with nothing left to reverse is `409 ALREADY_REVERSED`.

    `source` is `reversal` for every document but an invoice, whose
    cancellation is `invoice_cancel`: that source is in the unique constraint
    on (source, document), so the database itself refuses a second one.
    """
    if source not in (MovementSource.REVERSAL, MovementSource.INVOICE_CANCEL):
        raise _invalid("source", "Not a source a reversal can have.")
    try:
        with transaction.atomic():
            originals = list(
                Movement.objects.select_for_update(of=("self",))
                .filter(
                    document_type=document_type(document),
                    document_id=document.pk,
                    reversed_by__isnull=True,
                    # A mirror is never an original, whatever its source.
                    reverses__isnull=True,
                )
                .order_by("date", "created_at", "id")
            )
            if not originals:
                raise Conflict(ErrorCode.ALREADY_REVERSED)
            check_date(_fresh_company(originals[0].company_id), date)
            return [
                Movement.objects.create(
                    company_id=original.company_id,
                    account_id=original.account_id,
                    cash_account_id=original.cash_account_id,
                    date=date,
                    direction=_MIRROR_DIRECTION[original.direction],
                    cash_direction=_MIRROR_CASH_DIRECTION[original.cash_direction],
                    amount=original.amount,
                    currency=original.currency,
                    source=source,
                    document_type=original.document_type,
                    document_id=original.document_id,
                    reverses=original,
                    description=original.description,
                    created_by=by,
                    updated_by=by,
                )
                for original in originals
            ]
    except IntegrityError as exc:
        # Another request reversed one of these between our read and our write;
        # `reverses` is unique (and so is an invoice's cancellation), so the
        # database refused the second mirror.
        raise Conflict(ErrorCode.ALREADY_REVERSED) from exc
