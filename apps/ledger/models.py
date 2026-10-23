"""The ledger (F0): who owes the firm, whom the firm owes, and where its money is.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. Money is recorded in one
place only, `Movement`. A balance is never stored: it is the sum of the
movements, computed by apps.ledger.balances.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models

from core.models import CompanyOwnedModel

DEFAULT_CURRENCY = "TRY"


class AccountKind(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    SUPPLIER = "supplier", "Supplier"
    STAFF = "staff", "Staff"


class CashKind(models.TextChoices):
    CASH = "cash", "Cash"
    BANK = "bank", "Bank"
    CARD = "card", "Card"


class MovementSource(models.TextChoices):
    INVOICE = "invoice", "Invoice"
    INVOICE_CANCEL = "invoice_cancel", "Invoice cancelled"
    COLLECTION = "collection", "Collection"
    PAYMENT_OUT = "payment_out", "Payment out"
    MANUAL = "manual", "Manual entry"
    TRANSFER = "transfer", "Transfer"
    OPENING = "opening", "Opening balance"
    REVERSAL = "reversal", "Reversal"


class Direction(models.TextChoices):
    #: The party owes the firm more.
    DEBIT = "debit", "Debit"
    #: The party owes the firm less.
    CREDIT = "credit", "Credit"


class CashDirection(models.TextChoices):
    IN = "in", "In"
    OUT = "out", "Out"


class Supplier(CompanyOwnedModel):
    """Whom the firm buys from. The minimum F4 needs to buy parts and F0 needs
    to record what is owed; tax number and phone are checked by the serializer
    with the validators customers use."""

    name = models.CharField(max_length=200)
    tax_office = models.CharField(max_length=100, blank=True)
    tax_number = models.CharField(max_length=11, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(max_length=150, blank=True)
    address_note = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "ledger_supplier"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class Account(CompanyOwnedModel):
    """A party's running account with the firm (cari hesap).

    Exactly one of `customer`, `supplier` and `user` is set, the one `kind`
    names. A positive balance means the party owes the firm.
    """

    kind = models.CharField(max_length=20, choices=AccountKind.choices)
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="ledger_accounts",
        null=True,
        blank=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="accounts",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ledger_accounts",
        null=True,
        blank=True,
    )
    #: Copied from the party, and kept in step when the party is renamed.
    name = models.CharField(max_length=200)
    #: ISO 4217. Fixed once the account has a movement.
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    #: Inactive accounts are hidden from pickers, never deleted.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "ledger_account"
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=AccountKind.values), name="ledger_account_kind_valid"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind=AccountKind.CUSTOMER,
                        customer__isnull=False,
                        supplier__isnull=True,
                        user__isnull=True,
                    )
                    | models.Q(
                        kind=AccountKind.SUPPLIER,
                        customer__isnull=True,
                        supplier__isnull=False,
                        user__isnull=True,
                    )
                    | models.Q(
                        kind=AccountKind.STAFF,
                        customer__isnull=True,
                        supplier__isnull=True,
                        user__isnull=False,
                    )
                ),
                name="ledger_account_one_party",
            ),
            # One account per party and currency: a contract can be in any
            # currency, and an amount is only ever added to one in the same
            # currency. Live rows only, as for every unique key on a
            # soft-deleted model (core.models.SoftDeleteModel).
            models.UniqueConstraint(
                fields=["customer", "currency"],
                condition=models.Q(is_deleted=False, customer__isnull=False),
                name="uq_ledger_account_customer_currency",
            ),
            models.UniqueConstraint(
                fields=["supplier", "currency"],
                condition=models.Q(is_deleted=False, supplier__isnull=False),
                name="uq_ledger_account_supplier_currency",
            ),
            models.UniqueConstraint(
                fields=["user", "currency"],
                condition=models.Q(is_deleted=False, user__isnull=False),
                name="uq_ledger_account_user_currency",
            ),
        ]
        indexes = [models.Index(fields=["company", "kind"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


class CashAccountRole(models.TextChoices):
    #: The firm's main till, "Merkez kasa", made at registration. Where a
    #: payment with no cash account named goes, whatever the firm renames it.
    DEFAULT = "default", "Default till"
    #: Payments M7 recorded before the ledger existed ("Devir"), carried over
    #: by the data migration (apps.ledger.migration_helpers). One per currency.
    CARRIED_OVER = "carried_over", "Carried over from M7"


class CashAccount(CompanyOwnedModel):
    """Where the firm's money actually is: a till, a bank account, a card POS."""

    kind = models.CharField(max_length=20, choices=CashKind.choices)
    name = models.CharField(max_length=200)
    #: Set on a cash account the system made and looks up again, so it is found
    #: by what it is for rather than by a name the firm may change. Blank for
    #: every cash account a person creates.
    role = models.CharField(max_length=20, choices=CashAccountRole.choices, blank=True)
    #: ISO 4217. Fixed once used.
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    #: Optional, for `bank`.
    iban = models.CharField(max_length=34, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "ledger_cash_account"
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=CashKind.values), name="ledger_cash_account_kind_valid"
            ),
            # One default till per firm, in the firm's currency.
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(is_deleted=False, role=CashAccountRole.DEFAULT),
                name="uq_ledger_cash_account_default",
            ),
            # One "Devir" till per firm and currency: a carried-over payment
            # goes into a till in the invoice's own currency.
            models.UniqueConstraint(
                fields=["company", "currency"],
                condition=models.Q(is_deleted=False, role=CashAccountRole.CARRIED_OVER),
                name="uq_ledger_cash_account_carried_over",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


class MovementImmutableError(RuntimeError):
    """Raised when code tries to change or remove a movement.

    A programming error, not a request error: a mistake in the books is
    corrected by a reversal (apps.ledger.posting.reverse_document).
    """


class Movement(CompanyOwnedModel):
    """One amount of money, on a party's account, a cash account, or both.

    The only place money is recorded. Never updated and never deleted: a
    correction is a reversal (`source=reversal`, `reverses` set), and each
    movement can be reversed once.
    """

    #: The party side. Null only for a pure cash transfer.
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="movements", null=True, blank=True
    )
    #: Set when real money moved in or out of the firm.
    cash_account = models.ForeignKey(
        CashAccount, on_delete=models.PROTECT, related_name="movements", null=True, blank=True
    )
    #: The business date (Istanbul).
    date = models.DateField()
    #: Set with `account`, blank without one.
    direction = models.CharField(max_length=10, choices=Direction.choices, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    #: Set with `cash_account`, blank without one.
    cash_direction = models.CharField(max_length=10, choices=CashDirection.choices, blank=True)
    source = models.CharField(max_length=20, choices=MovementSource.choices)
    #: The source document, as "app_label.model_name" and its id.
    document_type = models.CharField(max_length=40, blank=True)
    document_id = models.UUIDField(null=True, blank=True)
    #: The movement this one cancels. One-to-one: a movement is reversed once.
    reverses = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="reversed_by", null=True, blank=True
    )
    description = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "ledger_movement"
        ordering = ["date", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="ledger_movement_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=MovementSource.values),
                name="ledger_movement_source_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(account__isnull=False) | models.Q(cash_account__isnull=False),
                name="ledger_movement_has_a_side",
            ),
            # A direction exactly when there is an account to apply it to.
            models.CheckConstraint(
                condition=(
                    models.Q(account__isnull=False, direction__in=Direction.values)
                    | models.Q(account__isnull=True, direction="")
                ),
                name="ledger_movement_direction_with_account",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cash_account__isnull=False, cash_direction__in=CashDirection.values)
                    | models.Q(cash_account__isnull=True, cash_direction="")
                ),
                name="ledger_movement_cash_direction_with_cash_account",
            ),
            # One invoice posts once, and is cancelled once.
            models.UniqueConstraint(
                fields=["source", "document_type", "document_id"],
                condition=models.Q(
                    source__in=[MovementSource.INVOICE, MovementSource.INVOICE_CANCEL]
                ),
                name="uq_ledger_movement_invoice_posts_once",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "date"]),
            models.Index(fields=["cash_account", "date"]),
            models.Index(fields=["document_type", "document_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.source} {self.amount} {self.currency}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Written once, by apps.ledger.posting, and never again. A queryset
        # update() goes around this; nothing in the code base issues one.
        if not self._state.adding:
            raise MovementImmutableError(f"Movement {self.pk} cannot be changed; reverse it.")
        super().save(*args, **kwargs)

    def delete(self, using: str | None = None, keep_parents: bool = False) -> Any:
        raise MovementImmutableError(f"Movement {self.pk} cannot be deleted; reverse it.")

    def hard_delete(self, using: str | None = None) -> Any:
        raise MovementImmutableError(f"Movement {self.pk} cannot be deleted; reverse it.")


# Documents ----------------------------------------------------------------------
#
# Each document below posts its movements through apps.ledger.posting and is
# never edited. Cancelling one posts the reversal of its movements and stamps
# `cancelled_at`; the row stays, so the movements keep naming their source.


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Transfer"
    CARD = "card", "Card"


class TransferKind(models.TextChoices):
    #: A balance moved from one party's account to another's.
    ACCOUNT = "account", "Between accounts"
    #: Money moved from one cash account to another, say the till to the bank.
    CASH = "cash", "Between cash accounts"


def _amount_positive(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=models.Q(amount__gt=0), name=name)


class DocumentCounter(CompanyOwnedModel):
    """The last number a firm gave out under one prefix ("T-", "O-").

    Incremented under a row lock by apps.ledger.services.next_number, so two
    documents recorded at once never share a number.
    """

    prefix = models.CharField(max_length=10)
    last = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "ledger_document_counter"
        ordering = ["prefix", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "prefix"], name="uq_ledger_document_counter_prefix"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.prefix}{self.last}"


class Collection(CompanyOwnedModel):
    """Money received from a party (tahsilat): a credit on the account, cash in.

    It may pay one or more invoices (`CollectionApplication`), or none: a
    collection with nothing applied is an advance, shown as unallocated.
    """

    number = models.CharField(max_length=20)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="collections")
    cash_account = models.ForeignKey(
        CashAccount, on_delete=models.PROTECT, related_name="collections"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    date = models.DateField()
    note = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_collection"
        ordering = ["-date", "-number", "-id"]
        constraints = [
            _amount_positive("ledger_collection_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(method__in=PaymentMethod.values),
                name="ledger_collection_method_valid",
            ),
            models.UniqueConstraint(
                fields=["company", "number"], name="uq_ledger_collection_number"
            ),
        ]
        indexes = [models.Index(fields=["account", "date"])]

    def __str__(self) -> str:
        return self.number


class CollectionApplication(CompanyOwnedModel):
    """The part of a collection that pays one invoice."""

    collection = models.ForeignKey(
        Collection, on_delete=models.PROTECT, related_name="applications"
    )
    invoice = models.ForeignKey(
        "billing.InvoiceDraft", on_delete=models.PROTECT, related_name="collection_applications"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = "ledger_collection_application"
        ordering = ["collection", "id"]
        constraints = [
            _amount_positive("ledger_collection_application_amount_positive"),
            models.UniqueConstraint(
                fields=["collection", "invoice"], name="uq_ledger_collection_application"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.collection_id} -> {self.invoice_id}: {self.amount}"


class PaymentOut(CompanyOwnedModel):
    """Money paid out to a party (tediye): a debit on the account, cash out."""

    number = models.CharField(max_length=20)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="payments_out")
    cash_account = models.ForeignKey(
        CashAccount, on_delete=models.PROTECT, related_name="payments_out"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    date = models.DateField()
    note = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_payment_out"
        ordering = ["-date", "-number", "-id"]
        constraints = [
            _amount_positive("ledger_payment_out_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(method__in=PaymentMethod.values),
                name="ledger_payment_out_method_valid",
            ),
            models.UniqueConstraint(
                fields=["company", "number"], name="uq_ledger_payment_out_number"
            ),
        ]
        indexes = [models.Index(fields=["account", "date"])]

    def __str__(self) -> str:
        return self.number


class ManualEntry(CompanyOwnedModel):
    """A debit or credit on an account with no cash behind it, and a reason."""

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="manual_entries")
    direction = models.CharField(max_length=10, choices=Direction.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField()
    reason = models.TextField()
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_manual_entry"
        ordering = ["-date", "-created_at", "-id"]
        constraints = [
            _amount_positive("ledger_manual_entry_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(direction__in=Direction.values),
                name="ledger_manual_entry_direction_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.direction} {self.amount}"


class Transfer(CompanyOwnedModel):
    """A transfer (virman): between two accounts, or between two cash accounts.

    Between accounts, the `from_account` is credited and the `to_account`
    debited. Between cash accounts, cash goes out of `from_cash` and in to
    `to_cash`, and no party's account moves.
    """

    kind = models.CharField(max_length=10, choices=TransferKind.choices)
    from_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="transfers_out",
        null=True,
        blank=True,
    )
    to_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="transfers_in",
        null=True,
        blank=True,
    )
    from_cash = models.ForeignKey(
        CashAccount,
        on_delete=models.PROTECT,
        related_name="transfers_out",
        null=True,
        blank=True,
    )
    to_cash = models.ForeignKey(
        CashAccount,
        on_delete=models.PROTECT,
        related_name="transfers_in",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField()
    note = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_transfer"
        ordering = ["-date", "-created_at", "-id"]
        constraints = [
            _amount_positive("ledger_transfer_amount_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind=TransferKind.ACCOUNT,
                        from_account__isnull=False,
                        to_account__isnull=False,
                        from_cash__isnull=True,
                        to_cash__isnull=True,
                    )
                    | models.Q(
                        kind=TransferKind.CASH,
                        from_account__isnull=True,
                        to_account__isnull=True,
                        from_cash__isnull=False,
                        to_cash__isnull=False,
                    )
                ),
                name="ledger_transfer_sides_match_kind",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_account=models.F("to_account"))
                & ~models.Q(from_cash=models.F("to_cash")),
                name="ledger_transfer_two_different_sides",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.kind} {self.amount}"


class OpeningBalance(CompanyOwnedModel):
    """A balance carried over at go-live (devir), on an account or a cash account.

    One live opening per side, entered before any other movement on it. On a
    cash account the direction reads as it does in bookkeeping: a debit is
    money in the till, a credit an overdraft.
    """

    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="openings", null=True, blank=True
    )
    cash_account = models.ForeignKey(
        CashAccount, on_delete=models.PROTECT, related_name="openings", null=True, blank=True
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField()
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_opening_balance"
        ordering = ["-date", "-created_at", "-id"]
        constraints = [
            _amount_positive("ledger_opening_balance_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(direction__in=Direction.values),
                name="ledger_opening_balance_direction_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(account__isnull=False, cash_account__isnull=True)
                    | models.Q(account__isnull=True, cash_account__isnull=False)
                ),
                name="ledger_opening_balance_one_side",
            ),
            models.UniqueConstraint(
                fields=["account"],
                condition=models.Q(
                    account__isnull=False, cancelled_at__isnull=True, is_deleted=False
                ),
                name="uq_ledger_opening_balance_account",
            ),
            models.UniqueConstraint(
                fields=["cash_account"],
                condition=models.Q(
                    cash_account__isnull=False, cancelled_at__isnull=True, is_deleted=False
                ),
                name="uq_ledger_opening_balance_cash_account",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date} opening {self.direction} {self.amount}"
