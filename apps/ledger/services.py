"""Ledger services: accounts, cash accounts, and the documents that move money.

Writing movements is apps.ledger.posting's job alone: every document here
posts through `post` and cancels through `reverse_document`, inside one
transaction with the document itself, so a refused posting leaves no document
behind.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Min, Model, Sum
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.companies.models import Company
from apps.customers.models import Customer
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
    CashDirection,
    CashKind,
    Collection,
    CollectionApplication,
    Direction,
    DocumentCounter,
    ManualEntry,
    Movement,
    MovementSource,
    OpeningBalance,
    PaymentMethod,
    PaymentOut,
    Transfer,
    TransferKind,
)
from apps.ledger.posting import is_money, lock_sides, post, reverse_document
from apps.users.models import User
from core.error_codes import ErrorCode
from core.exceptions import Conflict, field_error
from core.scheduling import local_today

#: Every firm starts with one till of this name. Plain ASCII, so it can live
#: here rather than in core/data.
DEFAULT_CASH_ACCOUNT_NAME = "Merkez kasa"


def account_for_customer(customer: Customer) -> Account:
    """The customer's account, created on first ask.

    Called by the post_save receiver (apps.ledger.signals) and by M7 when an
    invoice posts, so a customer that somehow has none still gets one rather
    than failing the invoice.
    """
    # Looked up unscoped, by the customer the caller already holds: a customer
    # saved with no company in context (a script, a command) would otherwise
    # find no account through the scoped manager and try to make a second one.
    # The create still passes through the tenant guard.
    account = Account.unscoped.filter(customer=customer, is_deleted=False).first()
    if account is None:
        account = Account.objects.create(
            company_id=customer.company_id,
            kind=AccountKind.CUSTOMER,
            customer=customer,
            name=customer.legal_name,
        )
    return account


def create_default_cash_account(company: Company) -> CashAccount:
    """The firm's first till, "Merkez kasa". Idempotent."""
    # Unscoped for the same reason as above: registration has no company in
    # context until this company exists.
    till = CashAccount.unscoped.filter(
        company=company, kind=CashKind.CASH, name=DEFAULT_CASH_ACCOUNT_NAME, is_deleted=False
    ).first()
    if till is None:
        till = CashAccount.objects.create(
            company=company, kind=CashKind.CASH, name=DEFAULT_CASH_ACCOUNT_NAME
        )
    return till


# Documents ----------------------------------------------------------------------

COLLECTION_PREFIX = "T-"
PAYMENT_OUT_PREFIX = "O-"
_NUMBER_DIGITS = 6
_ZERO = Decimal("0.00")
#: Invoices a collection can pay: issued, whether or not already marked paid.
_PAYABLE_STATUSES = (InvoiceStatus.ISSUED, InvoiceStatus.PAID)


def _invalid(field: str, reason: str, code: str = "invalid") -> ValidationError:
    return ValidationError({field: ErrorDetail(reason, code=code)})


def _same_firm(company_id: UUID, **records: Model | None) -> None:
    """Refuse a record of another firm as if it did not exist, which to this
    firm it does not."""
    for field, record in records.items():
        if record is not None and getattr(record, "company_id", None) != company_id:
            raise _invalid(field, "No such record.", code="does_not_exist")


def next_number(company: Company, prefix: str) -> str:
    """The firm's next number under `prefix`: "T-000001", "T-000002", ...

    The counter row is locked for the rest of the caller's transaction, so a
    second document waits for the first and takes the number after it; a
    transaction that rolls back gives its number back.
    """
    with transaction.atomic():
        counter = (
            DocumentCounter.unscoped.select_for_update()
            .filter(company=company, prefix=prefix)
            .first()
        )
        if counter is None:
            try:
                with transaction.atomic():
                    counter = DocumentCounter.unscoped.create(company=company, prefix=prefix)
            except IntegrityError:
                # Another transaction created it first; wait for its lock.
                counter = DocumentCounter.unscoped.select_for_update().get(
                    company=company, prefix=prefix
                )
        counter.last += 1
        counter.save(update_fields=["last", "updated_at"])
    return f"{prefix}{counter.last:0{_NUMBER_DIGITS}d}"


def invoice_collected(draft: InvoiceDraft) -> Decimal:
    """What collections still standing have paid of this invoice."""
    total = CollectionApplication.unscoped.filter(
        invoice_id=draft.pk,
        is_deleted=False,
        collection__cancelled_at__isnull=True,
        collection__is_deleted=False,
    ).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or _ZERO).quantize(_ZERO)


def invoice_remaining(draft: InvoiceDraft) -> Decimal:
    """The invoice's total less what collections still standing have paid."""
    return (Decimal(draft.total) - invoice_collected(draft)).quantize(_ZERO)


def _lock[D: (Collection, PaymentOut, ManualEntry, Transfer, OpeningBalance)](document: D) -> D:
    """The document's row, locked, and refused if it is already cancelled."""
    locked = type(document).unscoped.select_for_update().get(pk=document.pk)
    if locked.cancelled_at is not None:
        raise Conflict(ErrorCode.ALREADY_REVERSED)
    return locked


def _cancel[D: (Collection, PaymentOut, ManualEntry, Transfer, OpeningBalance)](
    document: D, *, by: User
) -> D:
    with transaction.atomic():
        locked = _lock(document)
        reverse_document(locked, date=local_today(), by=by)
        locked.cancelled_at = timezone.now()
        locked.updated_by = by
        locked.save(update_fields=["cancelled_at", "updated_by", "updated_at"])
    return locked


def _apply(
    collection: Collection, applies: list[tuple[InvoiceDraft, Decimal]], *, by: User
) -> None:
    """Pay invoices from a collection, each up to what it still owes."""
    per_invoice: dict[UUID, Decimal] = defaultdict(lambda: _ZERO)
    for invoice, amount in applies:
        _same_firm(collection.company_id, applies=invoice)
        if not is_money(amount):
            raise _invalid("applies", "An amount applied is positive, to two decimal places.")
        per_invoice[invoice.pk] += amount
    if sum(per_invoice.values(), _ZERO) > collection.amount:
        raise _invalid("applies", "More is applied than the collection brought in.")

    account = collection.account
    # Locked in a fixed order, so two collections paying the same invoices
    # cannot each hold one and wait for the other.
    for pk in sorted(per_invoice):
        locked = (
            InvoiceDraft.objects.select_for_update(of=("self",))
            .filter(pk=pk, company_id=collection.company_id)
            .first()
        )
        if locked is None:
            raise _invalid("applies", "No such record.", code="does_not_exist")
        invoice = locked
        if (
            invoice.status not in _PAYABLE_STATUSES
            or invoice.cancelled_at is not None
            or account.kind != AccountKind.CUSTOMER
            or invoice.customer_id != account.customer_id
        ):
            raise _invalid("applies", "Only this customer's issued invoices can be paid.")
        if invoice.currency != account.currency:
            raise field_error("applies", ErrorCode.CURRENCY_MISMATCH)
        if per_invoice[pk] > invoice_remaining(invoice):
            raise field_error("applies", ErrorCode.OVERPAYMENT_ON_INVOICE)
        CollectionApplication.objects.create(
            company_id=collection.company_id,
            collection=collection,
            invoice=invoice,
            amount=per_invoice[pk],
            created_by=by,
            updated_by=by,
        )


def _check_amount(amount: Decimal) -> None:
    # posting checks this too, but only after the document row is written,
    # where the database's own check would answer first and less clearly.
    if not is_money(amount):
        raise _invalid("amount", "An amount is positive, to two decimal places.")


def _check_method(method: str) -> None:
    if method not in PaymentMethod.values:
        raise _invalid("method", "Not a payment method.")


def record_collection(
    *,
    account: Account,
    cash_account: CashAccount,
    amount: Decimal,
    method: str,
    date: Date,
    note: str,
    applies: list[tuple[InvoiceDraft, Decimal]],
    by: User,
) -> Collection:
    """Money in from a party: a credit on its account, cash in to the till."""
    _same_firm(account.company_id, cash_account=cash_account)
    _check_method(method)
    _check_amount(amount)
    with transaction.atomic():
        collection = Collection.objects.create(
            company_id=account.company_id,
            number=next_number(account.company, COLLECTION_PREFIX),
            account=account,
            cash_account=cash_account,
            amount=amount,
            method=method,
            date=date,
            note=note,
            created_by=by,
            updated_by=by,
        )
        # Applications first, so the invoices are locked before the money
        # posts; a posting refused after them rolls them back with the rest.
        _apply(collection, applies, by=by)
        post(
            account=account,
            cash_account=cash_account,
            direction=Direction.CREDIT,
            cash_direction=CashDirection.IN,
            amount=amount,
            currency=account.currency,
            date=date,
            source=MovementSource.COLLECTION,
            document=collection,
            description=f"Tahsilat {collection.number}",
            by=by,
        )
    return collection


def cancel_collection(c: Collection, *, by: User) -> Collection:
    """Reverse a collection. The invoices it paid owe that amount again."""
    return _cancel(c, by=by)


def record_payment_out(
    *,
    account: Account,
    cash_account: CashAccount,
    amount: Decimal,
    method: str,
    date: Date,
    note: str,
    by: User,
) -> PaymentOut:
    """Money out to a party: a debit on its account, cash out of the till."""
    _same_firm(account.company_id, cash_account=cash_account)
    _check_method(method)
    _check_amount(amount)
    with transaction.atomic():
        payment = PaymentOut.objects.create(
            company_id=account.company_id,
            number=next_number(account.company, PAYMENT_OUT_PREFIX),
            account=account,
            cash_account=cash_account,
            amount=amount,
            method=method,
            date=date,
            note=note,
            created_by=by,
            updated_by=by,
        )
        post(
            account=account,
            cash_account=cash_account,
            direction=Direction.DEBIT,
            cash_direction=CashDirection.OUT,
            amount=amount,
            currency=account.currency,
            date=date,
            source=MovementSource.PAYMENT_OUT,
            document=payment,
            description=f"Tediye {payment.number}",
            by=by,
        )
    return payment


def cancel_payment_out(p: PaymentOut, *, by: User) -> PaymentOut:
    return _cancel(p, by=by)


def record_manual_entry(
    *,
    account: Account,
    direction: str,
    amount: Decimal,
    date: Date,
    reason: str,
    by: User,
) -> ManualEntry:
    """A debit or credit on an account with no cash behind it."""
    if not reason.strip():
        raise _invalid("reason", "A manual entry needs a reason.", code="required")
    if direction not in Direction.values:
        raise _invalid("direction", "Not a direction.")
    _check_amount(amount)
    with transaction.atomic():
        entry = ManualEntry.objects.create(
            company_id=account.company_id,
            account=account,
            direction=direction,
            amount=amount,
            date=date,
            reason=reason.strip(),
            created_by=by,
            updated_by=by,
        )
        post(
            account=account,
            cash_account=None,
            direction=direction,
            cash_direction=None,
            amount=amount,
            currency=account.currency,
            date=date,
            source=MovementSource.MANUAL,
            document=entry,
            description=entry.reason[:500],
            by=by,
        )
    return entry


def cancel_manual_entry(e: ManualEntry, *, by: User) -> ManualEntry:
    return _cancel(e, by=by)


def record_transfer(
    *,
    kind: str,
    amount: Decimal,
    date: Date,
    note: str,
    by: User,
    from_account: Account | None = None,
    to_account: Account | None = None,
    from_cash: CashAccount | None = None,
    to_cash: CashAccount | None = None,
) -> Transfer:
    """Move a balance between two accounts, or money between two cash accounts."""
    source: Account | CashAccount | None
    target: Account | CashAccount | None
    if kind == TransferKind.ACCOUNT:
        if from_cash is not None or to_cash is not None:
            raise _invalid("kind", "A transfer between accounts names no cash account.")
        source, target, target_field = from_account, to_account, "to_account"
    elif kind == TransferKind.CASH:
        if from_account is not None or to_account is not None:
            raise _invalid("kind", "A transfer between cash accounts names no account.")
        source, target, target_field = from_cash, to_cash, "to_cash"
    else:
        raise _invalid("kind", "Not a transfer kind.")
    if source is None:
        source_field = "from_account" if kind == TransferKind.ACCOUNT else "from_cash"
        raise _invalid(source_field, "Required.", code="required")
    if target is None:
        raise _invalid(target_field, "Required.", code="required")
    _same_firm(source.company_id, **{target_field: target})
    if source.pk == target.pk:
        raise _invalid(target_field, "A transfer moves between two different sides.")
    if source.currency != target.currency:
        raise field_error(target_field, ErrorCode.CURRENCY_MISMATCH)
    _check_amount(amount)

    with transaction.atomic():
        # Both sides up front, in lock_sides' order: the legs below lock one
        # side each, and two opposite transfers must not take them crosswise.
        lock_sides(source, target)
        transfer = Transfer.objects.create(
            company_id=source.company_id,
            kind=kind,
            from_account=from_account,
            to_account=to_account,
            from_cash=from_cash,
            to_cash=to_cash,
            amount=amount,
            date=date,
            note=note,
            created_by=by,
            updated_by=by,
        )
        legs: list[tuple[Account | None, CashAccount | None, str | None, str | None]]
        if kind == TransferKind.ACCOUNT:
            legs = [
                (from_account, None, Direction.CREDIT, None),
                (to_account, None, Direction.DEBIT, None),
            ]
        else:
            legs = [
                (None, from_cash, None, CashDirection.OUT),
                (None, to_cash, None, CashDirection.IN),
            ]
        for account, cash_account, direction, cash_direction in legs:
            post(
                account=account,
                cash_account=cash_account,
                direction=direction,
                cash_direction=cash_direction,
                amount=amount,
                currency=source.currency,
                date=date,
                source=MovementSource.TRANSFER,
                document=transfer,
                description=note[:500] or "Virman",
                by=by,
            )
    return transfer


def cancel_transfer(t: Transfer, *, by: User) -> Transfer:
    return _cancel(t, by=by)


def record_opening(
    *,
    direction: str,
    amount: Decimal,
    date: Date,
    by: User,
    account: Account | None = None,
    cash_account: CashAccount | None = None,
) -> OpeningBalance:
    """A balance carried over at go-live, dated on or before anything else on
    that side. Once it stands, posting refuses anything dated before it.

    On a cash account a debit is money in the till and a credit money out.
    """
    if (account is None) == (cash_account is None):
        raise _invalid("account", "An opening is on an account or on a cash account.")
    if direction not in Direction.values:
        raise _invalid("direction", "Not a direction.")
    _check_amount(amount)
    side: Account | CashAccount = account if account is not None else cash_account  # type: ignore[assignment]
    field = "account" if account is not None else "cash_account"

    with transaction.atomic():
        # The same lock every posting takes (posting.lock_sides): a movement
        # committing now either finished before this read, and is seen, or
        # waits for this opening and is then held to its date.
        lock_sides(side)
        openings = OpeningBalance.unscoped.filter(is_deleted=False, **{field: side})
        if openings.filter(cancelled_at__isnull=True).exists():
            raise _invalid(field, "This side already has an opening balance.", code="unique")
        earliest = (
            Movement.unscoped.filter(reversed_by__isnull=True, **{field: side})
            .exclude(source=MovementSource.REVERSAL)
            .aggregate(earliest=Min("date"))["earliest"]
        )
        if earliest is not None and date > earliest:
            raise _invalid("date", "Opening balance must come first")

        opening = OpeningBalance.objects.create(
            company_id=side.company_id,
            account=account,
            cash_account=cash_account,
            direction=direction,
            amount=amount,
            date=date,
            created_by=by,
            updated_by=by,
        )
        is_debit = direction == Direction.DEBIT
        post(
            account=account,
            cash_account=cash_account,
            direction=direction if account is not None else None,
            cash_direction=(
                None
                if cash_account is None
                else (CashDirection.IN if is_debit else CashDirection.OUT)
            ),
            amount=amount,
            currency=side.currency,
            date=date,
            source=MovementSource.OPENING,
            document=opening,
            description="Devir",
            by=by,
        )
    return opening


def cancel_opening(o: OpeningBalance, *, by: User) -> OpeningBalance:
    return _cancel(o, by=by)
