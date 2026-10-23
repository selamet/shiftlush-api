"""The ledger's request and response shapes.

Every foreign key a request names is looked up in the requesting firm only:
the field's queryset is set per request in `get_fields`, through the tenant
manager, so another firm's id reads `does_not_exist` exactly as a made-up one
does (the billing serializers do the same). Users are the one exception: their
manager is not tenant-scoped, so they are narrowed by the requester's company.

The annotated `balance` is read in one way everywhere, through a
`SerializerMethodField` that falls back to the per-row helper when the row did
not come from an annotated queryset (a record just created).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.billing.models import InvoiceDraft
from apps.customers.models import Customer
from apps.ledger.balances import account_balance, cash_balance
from apps.ledger.finance import PERIODS
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
    CashKind,
    Collection,
    CollectionApplication,
    Direction,
    ManualEntry,
    Movement,
    OpeningBalance,
    PaymentMethod,
    PaymentOut,
    Supplier,
    Transfer,
    TransferKind,
)
from apps.users.models import User
from core.error_codes import ErrorCode
from core.exceptions import field_error
from core.scheduling import local_today
from core.validators import normalize_email, normalize_phone, validate_tax_number


class StrictMixin:
    """Rejects a field the client sent that this serializer does not declare.

    Copied rather than imported, as in every other app.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


def _money(value: Decimal) -> str:
    return f"{Decimal(value):.2f}"


def _scope(fields: dict[str, Any], name: str, queryset: Any) -> None:
    """Point one related field at this request's firm."""
    cast(serializers.PrimaryKeyRelatedField[Any], fields[name]).queryset = queryset


def _related(model: type[Any], help_text: str, **kwargs: Any) -> Any:
    # Scoped per request in get_fields; none() until then, so a field read
    # outside a request (the schema generator) can match nothing.
    return serializers.PrimaryKeyRelatedField(
        queryset=model.objects.none(), help_text=help_text, **kwargs
    )


_BALANCE = serializers.DecimalField(
    max_digits=16,
    decimal_places=2,
    read_only=True,
    help_text="Debits less credits. Positive: the party owes the firm.",
)
_CASH_BALANCE = serializers.DecimalField(
    max_digits=16,
    decimal_places=2,
    read_only=True,
    help_text="What came in less what went out.",
)


# Reads ----------------------------------------------------------------------------


class MovementSerializer(serializers.ModelSerializer[Movement]):
    class Meta:
        model = Movement
        fields = [
            "id",
            "date",
            "account",
            "cash_account",
            "direction",
            "cash_direction",
            "amount",
            "currency",
            "source",
            "document_type",
            "document_id",
            "reverses",
            "description",
            "created_at",
        ]
        read_only_fields = fields


class AccountReadSerializer(serializers.ModelSerializer[Account]):
    balance = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = [
            "id",
            "kind",
            "name",
            "customer",
            "supplier",
            "user",
            "currency",
            "is_active",
            "balance",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(_BALANCE)
    def get_balance(self, account: Account) -> str:
        balance = getattr(account, "balance", None)
        return _money(account_balance(account) if balance is None else balance)


class AccountDetailSerializer(AccountReadSerializer):
    last_movement = serializers.SerializerMethodField()

    class Meta(AccountReadSerializer.Meta):
        fields = [*AccountReadSerializer.Meta.fields, "last_movement"]
        read_only_fields = fields

    @extend_schema_field(MovementSerializer(allow_null=True))
    def get_last_movement(self, account: Account) -> dict[str, Any] | None:
        last = Movement.objects.filter(account=account).order_by("-date", "-created_at", "-id")
        movement = last.first()
        return None if movement is None else dict(MovementSerializer(movement).data)


class AccountTotalsSerializer(serializers.Serializer[Any]):
    receivables = serializers.DecimalField(
        max_digits=14, decimal_places=2, help_text="The positive balances, summed."
    )
    payables = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="The negative balances, summed, as a positive amount.",
    )
    net = serializers.DecimalField(
        max_digits=14, decimal_places=2, help_text="Receivables less payables."
    )


class CashAccountReadSerializer(serializers.ModelSerializer[CashAccount]):
    balance = serializers.SerializerMethodField()

    class Meta:
        model = CashAccount
        fields = [
            "id",
            "kind",
            "name",
            "role",
            "currency",
            "iban",
            "is_active",
            "balance",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(_CASH_BALANCE)
    def get_balance(self, cash: CashAccount) -> str:
        balance = getattr(cash, "balance", None)
        return _money(cash_balance(cash) if balance is None else balance)


class CashAccountPickerSerializer(serializers.ModelSerializer[CashAccount]):
    """A cash account to pick, for a role that records money but does not read
    the ledger (operations): what it is, never what it holds or its IBAN."""

    class Meta:
        model = CashAccount
        fields = ["id", "name", "kind", "currency", "is_active"]
        read_only_fields = fields


class SupplierReadSerializer(serializers.ModelSerializer[Supplier]):
    account = serializers.SerializerMethodField(help_text="The supplier's account.")

    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "tax_office",
            "tax_number",
            "phone",
            "email",
            "address_note",
            "notes",
            "account",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_account(self, supplier: Supplier) -> str | None:
        account = next(iter(supplier.accounts.all()), None)
        return None if account is None else str(account.pk)


class CollectionApplicationSerializer(serializers.ModelSerializer[CollectionApplication]):
    class Meta:
        model = CollectionApplication
        fields = ["invoice", "amount"]
        read_only_fields = fields


class CollectionReadSerializer(serializers.ModelSerializer[Collection]):
    applies = CollectionApplicationSerializer(source="applications", many=True, read_only=True)

    class Meta:
        model = Collection
        fields = [
            "id",
            "number",
            "account",
            "cash_account",
            "amount",
            "method",
            "date",
            "note",
            "applies",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


class PaymentOutReadSerializer(serializers.ModelSerializer[PaymentOut]):
    class Meta:
        model = PaymentOut
        fields = [
            "id",
            "number",
            "account",
            "cash_account",
            "amount",
            "method",
            "date",
            "note",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


class ManualEntryReadSerializer(serializers.ModelSerializer[ManualEntry]):
    class Meta:
        model = ManualEntry
        fields = [
            "id",
            "account",
            "direction",
            "amount",
            "date",
            "reason",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


class TransferReadSerializer(serializers.ModelSerializer[Transfer]):
    class Meta:
        model = Transfer
        fields = [
            "id",
            "kind",
            "from_account",
            "to_account",
            "from_cash",
            "to_cash",
            "amount",
            "date",
            "note",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


class OpeningBalanceReadSerializer(serializers.ModelSerializer[OpeningBalance]):
    class Meta:
        model = OpeningBalance
        fields = [
            "id",
            "account",
            "cash_account",
            "direction",
            "amount",
            "date",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


# Writes ---------------------------------------------------------------------------


class AccountCreateSerializer(StrictMixin, serializers.Serializer[Any]):
    """Open an account for a party that has none: a staff member, mostly.

    A customer's account comes with the customer, and a supplier's with the
    supplier; asking again for one that exists is `unique`.
    """

    kind = serializers.ChoiceField(choices=AccountKind.choices)
    customer = _related(Customer, "For `customer`.", required=False)
    supplier = _related(Supplier, "For `supplier`.", required=False)
    user = _related(User, "For `staff`: a member of the firm.", required=False)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False, default="TRY")

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        _scope(fields, "customer", Customer.objects.all())
        _scope(fields, "supplier", Supplier.objects.all())
        # Users are not tenant-scoped by their manager; narrowed here to the
        # requester's firm. Outside a request (the schema generator), none.
        requester = getattr(self.context.get("request"), "user", None)
        users = User.objects.none()
        if isinstance(requester, User) and requester.company_id is not None:
            users = User.objects.filter(company_id=requester.company_id, is_deleted=False)
        _scope(fields, "user", users)
        return fields


class CashAccountWriteSerializer(StrictMixin, serializers.ModelSerializer[CashAccount]):
    kind = serializers.ChoiceField(choices=CashKind.choices)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False)

    class Meta:
        model = CashAccount
        fields = ["kind", "name", "currency", "iban", "is_active"]

    def validate_currency(self, value: str) -> str:
        cash = self.instance
        if (
            isinstance(cash, CashAccount)
            and value != cash.currency
            and Movement.objects.filter(cash_account=cash).exists()
        ):
            raise serializers.ValidationError(
                "The currency is fixed once the cash account is used.", code="invalid"
            )
        return value


class SupplierWriteSerializer(StrictMixin, serializers.ModelSerializer[Supplier]):
    class Meta:
        model = Supplier
        fields = ["name", "tax_office", "tax_number", "phone", "email", "address_note", "notes"]

    def validate_tax_number(self, value: str) -> str:
        return validate_tax_number(value) if value else value

    def validate_phone(self, value: str) -> str:
        return normalize_phone(value) if value else value

    def validate_email(self, value: str) -> str:
        return normalize_email(value) if value else value


class _DocumentWriteSerializer(StrictMixin, serializers.Serializer[Any]):
    """What every document takes: an amount and a business date."""

    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, help_text="Above zero, to the cent."
    )
    date = serializers.DateField(
        required=False,
        default=local_today,
        help_text="The business date: not after today, nor in a closed period. Today when omitted.",
    )

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        for name, field in fields.items():
            if not isinstance(field, serializers.PrimaryKeyRelatedField):
                continue
            model = cast(Any, field).queryset.model
            if model is Account:
                # Inactive accounts are hidden from pickers, and take no new
                # documents either, as inactive cash accounts.
                _scope(fields, name, Account.objects.filter(is_active=True))
            elif model is CashAccount:
                # Inactive cash accounts are hidden from pickers, and take no
                # new money either.
                _scope(fields, name, CashAccount.objects.filter(is_active=True))
        return fields


class ApplySerializer(serializers.Serializer[Any]):
    invoice = _related(InvoiceDraft, "An issued invoice of the collection's customer.")
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, help_text="What this collection pays of it."
    )

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        _scope(fields, "invoice", InvoiceDraft.objects.all())
        return fields


class CollectionApplySerializer(StrictMixin, serializers.Serializer[Any]):
    applies = ApplySerializer(
        many=True,
        allow_empty=False,
        help_text=(
            "The invoices it pays, each up to what it still owes, all together up "
            "to what the collection has not yet applied."
        ),
    )


class OpenInvoiceSerializer(serializers.ModelSerializer[InvoiceDraft]):
    """An invoice a collection can still pay, as the picker lists it."""

    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    remaining = serializers.DecimalField(
        max_digits=16,
        decimal_places=2,
        read_only=True,
        help_text="The total less what standing collections pay of it.",
    )

    class Meta:
        model = InvoiceDraft
        fields = ["id", "invoice_number", "invoice_date", "total", "remaining"]
        read_only_fields = fields


class CollectionCreateSerializer(_DocumentWriteSerializer):
    account = _related(Account, "The party's account.")
    cash_account = _related(CashAccount, "Where the money went.")
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    applies = ApplySerializer(
        many=True,
        required=False,
        default=list,
        help_text="The invoices it pays, each up to what it still owes. None: an advance.",
    )


class PaymentOutCreateSerializer(_DocumentWriteSerializer):
    account = _related(Account, "The party's account.")
    cash_account = _related(CashAccount, "Where the money came from.")
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class ManualEntryCreateSerializer(_DocumentWriteSerializer):
    account = _related(Account, "The party's account.")
    direction = serializers.ChoiceField(choices=Direction.choices)
    reason = serializers.CharField(help_text="Why; shown on the statement.")


class TransferCreateSerializer(_DocumentWriteSerializer):
    kind = serializers.ChoiceField(choices=TransferKind.choices)
    from_account = _related(Account, "For `account`: credited.", required=False)
    to_account = _related(Account, "For `account`: debited.", required=False)
    from_cash = _related(CashAccount, "For `cash`: money out.", required=False)
    to_cash = _related(CashAccount, "For `cash`: money in.", required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class OpeningBalanceCreateSerializer(_DocumentWriteSerializer):
    account = _related(Account, "An account, or ...", required=False)
    cash_account = _related(CashAccount, "... a cash account.", required=False)
    direction = serializers.ChoiceField(
        choices=Direction.choices,
        help_text="On a cash account, a debit is money in the till.",
    )


class LedgerSettingsSerializer(StrictMixin, serializers.Serializer[Any]):
    closed_through = serializers.DateField(
        allow_null=True,
        help_text=(
            "The books are closed through this day, inclusive: nothing can be dated "
            "on or before it. Null: nothing is closed. Not after today."
        ),
    )


# The finance summary --------------------------------------------------------------


class FinanceQuerySerializer(serializers.Serializer[Any]):
    period = serializers.ChoiceField(choices=PERIODS, default="today", required=False)


class FinanceCashLineSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="cash_account.pk")
    name = serializers.CharField(source="cash_account.name")
    kind = serializers.ChoiceField(choices=CashKind.choices, source="cash_account.kind")
    is_active = serializers.BooleanField(
        source="cash_account.is_active",
        help_text="An inactive cash account is listed while it holds money or moved.",
    )
    balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="Its balance today."
    )
    money_in = serializers.DecimalField(max_digits=16, decimal_places=2)
    money_out = serializers.DecimalField(max_digits=16, decimal_places=2)


class FinanceCurrencySerializer(serializers.Serializer[Any]):
    currency = serializers.CharField()
    receivables = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="What the firm is owed, today."
    )
    payables = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="What the firm owes, today."
    )
    cash_accounts = FinanceCashLineSerializer(
        many=True,
        help_text="Every cash account that is active, holds money, or moved in the period.",
    )
    money_in = serializers.DecimalField(
        max_digits=16,
        decimal_places=2,
        help_text=(
            "The cash lines' `money_in`, added up. Transfers between cash accounts, "
            "openings, and documents cancelled within the period are left out."
        ),
    )
    money_out = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="The cash lines' `money_out`, added up."
    )


class FinanceSummarySerializer(serializers.Serializer[Any]):
    period = serializers.ChoiceField(choices=PERIODS)
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    currencies = FinanceCurrencySerializer(
        many=True, help_text="One entry per currency; amounts in two currencies are never added."
    )


# The statement ----------------------------------------------------------------------


class StatementQuerySerializer(serializers.Serializer[Any]):
    """The range of a statement, from the query string or the send body."""

    start = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Inclusive. The opening balance is everything dated before it.",
    )
    end = serializers.DateField(required=False, allow_null=True, help_text="Inclusive.")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("start"), attrs.get("end")
        if start is not None and end is not None and start > end:
            raise field_error("start", ErrorCode.END_DATE_BEFORE_START_DATE)
        return attrs


class StatementRowSerializer(serializers.Serializer[Any]):
    movement_id = serializers.UUIDField()
    date = serializers.DateField()
    description = serializers.CharField()
    debit = serializers.DecimalField(max_digits=16, decimal_places=2)
    credit = serializers.DecimalField(max_digits=16, decimal_places=2)
    balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="The account's balance after this row."
    )
    document_type = serializers.CharField(
        help_text='The source document as "app_label.model_name", e.g. "ledger.collection".'
    )
    document_id = serializers.UUIDField(allow_null=True)
    is_reversal = serializers.BooleanField(
        help_text="The mirror of an earlier row: a cancelled document shows as both."
    )


class StatementSerializer(serializers.Serializer[Any]):
    account = serializers.UUIDField(source="account.pk")
    account_name = serializers.CharField(source="account.name")
    currency = serializers.CharField()
    start = serializers.DateField(allow_null=True)
    end = serializers.DateField(allow_null=True)
    opening = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="The balance before `start`; 0 without one."
    )
    total_debit = serializers.DecimalField(max_digits=16, decimal_places=2)
    total_credit = serializers.DecimalField(max_digits=16, decimal_places=2)
    closing = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="The balance after the last row."
    )
    rows = StatementRowSerializer(many=True)


class StatementSentSerializer(serializers.Serializer[Any]):
    recipients = serializers.ListField(
        child=serializers.EmailField(), help_text="Each address the statement was sent to."
    )
