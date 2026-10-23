"""The ledger (F0): who owes the firm, whom the firm owes, and where its money is.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. Money is recorded in one
place only, `Movement`. A balance is never stored: it is the sum of the
movements, computed by apps.ledger.balances.
"""

from __future__ import annotations

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
            # One account per party. Live rows only, as for every unique key on
            # a soft-deleted model (core.models.SoftDeleteModel).
            models.UniqueConstraint(
                fields=["customer"],
                condition=models.Q(is_deleted=False, customer__isnull=False),
                name="uq_ledger_account_customer",
            ),
            models.UniqueConstraint(
                fields=["supplier"],
                condition=models.Q(is_deleted=False, supplier__isnull=False),
                name="uq_ledger_account_supplier",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_deleted=False, user__isnull=False),
                name="uq_ledger_account_user",
            ),
        ]
        indexes = [models.Index(fields=["company", "kind"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


class CashAccount(CompanyOwnedModel):
    """Where the firm's money actually is: a till, a bank account, a card POS."""

    kind = models.CharField(max_length=20, choices=CashKind.choices)
    name = models.CharField(max_length=200)
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
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


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
