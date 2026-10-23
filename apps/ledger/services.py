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
from django.db.models import Max, Min, Model, Sum
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.companies.models import Company
from apps.customers.models import Customer
from apps.ledger.models import (
    DEFAULT_CURRENCY,
    Account,
    AccountKind,
    CashAccount,
    CashAccountRole,
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
    Supplier,
    Transfer,
    TransferKind,
)
from apps.ledger.posting import document_type, is_money, lock_sides, post, reverse_document
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
    """The firm's first till, "Merkez kasa". Idempotent.

    Found by its role, not its name: a firm that renames its till still has
    one, and gets no second.
    """
    # Unscoped for the same reason as above: registration has no company in
    # context until this company exists.
    till = CashAccount.unscoped.filter(
        company=company, role=CashAccountRole.DEFAULT, is_deleted=False
    ).first()
    if till is None:
        till = CashAccount.objects.create(
            company=company,
            kind=CashKind.CASH,
            role=CashAccountRole.DEFAULT,
            name=DEFAULT_CASH_ACCOUNT_NAME,
        )
    return till


# Documents ----------------------------------------------------------------------
#
# LOCK ORDER. Every path that moves money takes its row locks in this order,
# and never goes back to an earlier kind once it has a later one:
#
#   1. invoice drafts, in pk order        (_lock_invoices, billing._locked)
#   2. the document row (collection, ...) (_lock)
#   3. the document counter               (next_number)
#   4. movements                          (posting.reverse_document)
#   5. accounts, then cash accounts, each in pk order (posting.lock_sides;
#      a movement insert also takes FOR KEY SHARE on its sides)
#
# A path holding a later lock while waiting for an earlier one is a deadlock
# on PostgreSQL against a path going the right way: cancel_collection used to
# hold the account (key share, from the mirror insert) and wait for the
# invoice, while mark_paid held the invoice and waited for the account. So the
# invoices a document touches are found without a lock and locked first, and
# what runs after that reads them plainly under the locks already held
# (_apply, sync_invoice_status). tests/test_ledger.py
# `test_every_path_locks_invoices_first` checks it.

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


def _lock_invoices(pks: set[UUID]) -> None:
    """Step 1 of the lock order: these invoices, in pk order."""
    if pks:
        list(InvoiceDraft.objects.select_for_update(of=("self",)).filter(pk__in=pks).order_by("pk"))


def _apply(
    collection: Collection, applies: list[tuple[InvoiceDraft, Decimal]], *, by: User | None
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
    # record_collection locked these first (the lock order above); read under
    # that lock, in the same order.
    for pk in sorted(per_invoice):
        locked = InvoiceDraft.objects.filter(pk=pk, company_id=collection.company_id).first()
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
    by: User | None,
) -> Collection:
    """Money in from a party: a credit on its account, cash in to the till."""
    _same_firm(account.company_id, cash_account=cash_account)
    _check_method(method)
    _check_amount(amount)
    with transaction.atomic():
        # The invoices first, in pk order, and only then the number counter:
        # the same order as mark_paid (invoice, then this), so the two never
        # hold one each and wait for the other.
        _lock_invoices({invoice.pk for invoice, _ in applies})
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
        # Each invoice it paid reads paid or issued by what it now owes; they
        # were locked by _apply, in pk order.
        for invoice_id in sorted({invoice.pk for invoice, _ in applies}):
            sync_invoice_status(InvoiceDraft.objects.get(pk=invoice_id))
    return collection


def cancel_collection(c: Collection, *, by: User) -> Collection:
    """Reverse a collection. The invoices it paid owe that amount again."""
    # Read without a lock: a collection's applications are written with it
    # and never change. Then the lock order: its invoices, then the collection
    # itself (_cancel re-checks under that lock that it is not cancelled yet).
    invoice_ids = set(
        CollectionApplication.unscoped.filter(collection_id=c.pk, is_deleted=False).values_list(
            "invoice_id", flat=True
        )
    )
    with transaction.atomic():
        _lock_invoices(invoice_ids)
        cancelled = _cancel(c, by=by)
        for invoice_id in sorted(invoice_ids):
            sync_invoice_status(InvoiceDraft.objects.get(pk=invoice_id))
    return cancelled


# Invoices (M7) ------------------------------------------------------------------


def _invoice_movement(draft: InvoiceDraft) -> Movement | None:
    return Movement.unscoped.filter(
        source=MovementSource.INVOICE,
        document_type=document_type(draft),
        document_id=draft.pk,
    ).first()


def _invoice_description(draft: InvoiceDraft) -> str:
    period = f"{draft.period_start:%d.%m.%Y}-{draft.period_end:%d.%m.%Y}"
    return f"Fatura {draft.invoice_number} — {period}"[:500]


def post_invoice(
    draft: InvoiceDraft, *, by: User | None, date: Date | None = None
) -> Movement | None:
    """The issued invoice's debit on its customer's account, once.

    Dated the invoice date (or `date`, which the carry-over migration passes
    for an invoice dated after today), for the invoice total. None when it is
    already posted (a retried issue, a re-run migration), and None for a zero
    total, which owes nothing and is not a movement. The unique constraint on
    (source, document) refuses a second debit even if two callers race.
    """
    with transaction.atomic():
        locked = InvoiceDraft.objects.select_for_update(of=("self",)).get(pk=draft.pk)
        if locked.status not in _PAYABLE_STATUSES or locked.cancelled_at is not None:
            raise _invalid("invoice", "Only an issued invoice posts to the account.")
        if locked.invoice_date is None:
            raise _invalid("invoice_date", "An issued invoice has a date.", code="required")
        if _invoice_movement(locked) is not None or Decimal(locked.total) == _ZERO:
            return None
        return post(
            account=account_for_customer(locked.customer),
            cash_account=None,
            direction=Direction.DEBIT,
            cash_direction=None,
            amount=Decimal(locked.total),
            currency=locked.currency,
            date=date or locked.invoice_date,
            source=MovementSource.INVOICE,
            document=locked,
            description=_invoice_description(locked),
            by=by,
        )


def reverse_invoice(draft: InvoiceDraft, *, by: User) -> None:
    """Cancel an issued invoice's debit, dated today, as `invoice_cancel`.

    Refused while a standing collection pays any of it: the collection is
    cancelled first (`409 INVOICE_HAS_COLLECTIONS`). An invoice that never
    posted (a zero total) has nothing to reverse.
    """
    with transaction.atomic():
        locked = InvoiceDraft.objects.select_for_update(of=("self",)).get(pk=draft.pk)
        if invoice_collected(locked) > _ZERO:
            raise Conflict(ErrorCode.INVOICE_HAS_COLLECTIONS)
        if _invoice_movement(locked) is None:
            return
        reverse_document(locked, date=local_today(), by=by, source=MovementSource.INVOICE_CANCEL)


def sync_invoice_status(draft: InvoiceDraft) -> InvoiceDraft:
    """`paid` when standing collections cover the invoice, `issued` otherwise.

    `paid_on` is the date of the collection that covered it: the latest of
    those still applied. An invoice for nothing (a zero total) owes nothing
    from the moment it is issued, so it is `paid` on its invoice date. Called
    at issue, and by every collection and cancellation that touches the
    invoice, in the same transaction. A draft or a cancelled invoice is left as
    it is.

    The caller holds the invoice's lock already: every caller takes it first,
    per the lock order above, so this reads it plainly rather than locking it
    again after the sides.
    """
    locked = InvoiceDraft.objects.get(pk=draft.pk)
    if locked.status not in _PAYABLE_STATUSES or locked.cancelled_at is not None:
        return locked
    status: str
    paid_on: Date | None
    if Decimal(locked.total) == _ZERO:
        status, paid_on = InvoiceStatus.PAID, locked.invoice_date
    elif invoice_remaining(locked) <= _ZERO:
        latest = CollectionApplication.unscoped.filter(
            invoice_id=locked.pk,
            is_deleted=False,
            collection__cancelled_at__isnull=True,
            collection__is_deleted=False,
        ).aggregate(latest=Max("collection__date"))["latest"]
        status, paid_on = InvoiceStatus.PAID, latest or locked.invoice_date
    else:
        status, paid_on = InvoiceStatus.ISSUED, None
    if (locked.status, locked.paid_on) != (status, paid_on):
        locked.status = status
        locked.paid_on = paid_on
        locked.save(update_fields=["status", "paid_on", "updated_at"])
    return locked


def default_cash_account(company_id: UUID) -> CashAccount:
    """The firm's default till, where a payment with no till named goes."""
    return create_default_cash_account(Company.objects.get(pk=company_id))


def collect_invoice(
    draft: InvoiceDraft,
    *,
    date: Date,
    by: User | None,
    cash_account: CashAccount | None = None,
    method: str = PaymentMethod.TRANSFER,
    note: str = "",
) -> Collection:
    """A collection of everything the invoice still owes, applied to it."""
    remaining = invoice_remaining(draft)
    return record_collection(
        account=account_for_customer(draft.customer),
        cash_account=cash_account or default_cash_account(draft.company_id),
        amount=remaining,
        method=method,
        date=date,
        note=note,
        applies=[(draft, remaining)],
        by=by,
    )


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
        earliest = Movement.unscoped.filter(
            reversed_by__isnull=True, reverses__isnull=True, **{field: side}
        ).aggregate(earliest=Min("date"))["earliest"]
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


# Parties ------------------------------------------------------------------------


_PARTY_FIELD = {
    AccountKind.CUSTOMER.value: "customer",
    AccountKind.SUPPLIER.value: "supplier",
    AccountKind.STAFF.value: "user",
}


def _party_name(party: Customer | Supplier | User) -> str:
    if isinstance(party, Customer):
        return party.legal_name
    if isinstance(party, Supplier):
        return party.name
    return party.full_name or party.email


def open_account(
    *,
    company_id: UUID,
    kind: str,
    by: User,
    customer: Customer | None = None,
    supplier: Supplier | None = None,
    user: User | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> Account:
    """An account for a party that has none: a staff member above all, whose
    account is opened the first time they are given one. Exactly the party
    `kind` names, of this firm."""
    if kind not in _PARTY_FIELD:
        raise _invalid("kind", "Not an account kind.")
    parties: dict[str, Customer | Supplier | User | None] = {
        "customer": customer,
        "supplier": supplier,
        "user": user,
    }
    field = _PARTY_FIELD[kind]
    for other, value in parties.items():
        if other != field and value is not None:
            raise _invalid(other, "An account names one party, the one its kind names.")
    party = parties[field]
    if party is None:
        raise _invalid(field, "Required for this kind.", code="required")
    _same_firm(company_id, **{field: party})
    with transaction.atomic():
        if Account.unscoped.filter(is_deleted=False, **{field: party}).exists():
            raise _invalid(field, "This party already has an account.", code="unique")
        return Account.objects.create(
            company_id=company_id,
            kind=kind,
            customer=customer,
            supplier=supplier,
            user=user,
            name=_party_name(party)[:200],
            currency=currency,
            created_by=by,
            updated_by=by,
        )


def create_supplier(*, company_id: UUID, by: User, **fields: object) -> Supplier:
    """A supplier and its account, together or not at all."""
    with transaction.atomic():
        supplier = Supplier.objects.create(
            company_id=company_id, created_by=by, updated_by=by, **fields
        )
        Account.objects.create(
            company_id=company_id,
            kind=AccountKind.SUPPLIER,
            supplier=supplier,
            name=supplier.name,
            created_by=by,
            updated_by=by,
        )
    return supplier


def update_supplier(supplier: Supplier, *, changes: dict[str, object], by: User) -> Supplier:
    """Change a supplier; its account follows a rename, in the same write."""
    with transaction.atomic():
        for name, value in changes.items():
            setattr(supplier, name, value)
        supplier.updated_by = by
        supplier.save()
        for account in Account.objects.filter(supplier=supplier).exclude(name=supplier.name):
            # Saved rather than updated in bulk, so the audit log records it.
            account.name = supplier.name
            account.updated_by = by
            account.save(update_fields=["name", "updated_by", "updated_at"])
    return supplier
