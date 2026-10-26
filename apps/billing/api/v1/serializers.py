from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from rest_framework import serializers

from apps.billing.models import InvoiceDraft, InvoiceLine
from apps.ledger.models import CashAccount, PaymentMethod


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


class InvoiceLineSerializer(serializers.ModelSerializer[InvoiceLine]):
    class Meta:
        model = InvoiceLine
        fields = [
            "id",
            "draft",
            "kind",
            "description",
            "elevator",
            "fault",
            "quantity",
            "unit_price",
            "amount",
            "needs_price",
            "position",
        ]
        read_only_fields = fields


class InvoiceDraftReadSerializer(serializers.ModelSerializer[InvoiceDraft]):
    contract_number = serializers.CharField(source="contract.contract_number", read_only=True)
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    lines = InvoiceLineSerializer(many=True, read_only=True)
    needs_price = serializers.SerializerMethodField(
        help_text="Whether any line still waits for a price; such a draft cannot be issued."
    )
    collected = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        help_text="What collections still standing have paid of this invoice.",
    )
    remaining = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        help_text="The total less `collected`: what the customer still owes on it.",
    )

    class Meta:
        model = InvoiceDraft
        fields = [
            "id",
            "contract",
            "contract_number",
            "customer",
            "customer_name",
            "period_start",
            "period_end",
            "status",
            "currency",
            "vat_rate",
            "subtotal",
            "vat_amount",
            "total",
            "collected",
            "remaining",
            "needs_price",
            "invoice_number",
            "invoice_date",
            "paid_on",
            "cancelled_at",
            "cancel_reason",
            "notes",
            "lines",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_needs_price(self, draft: InvoiceDraft) -> bool:
        # From the prefetched lines: no query per row.
        return any(line.needs_price for line in draft.lines.all())


class InvoiceDraftEditSerializer(StrictMixin, serializers.ModelSerializer[InvoiceDraft]):
    class Meta:
        model = InvoiceDraft
        fields = ["notes"]


_POSITIVE = Decimal("0.0001")


class InvoiceLineEditSerializer(StrictMixin, serializers.ModelSerializer[InvoiceLine]):
    """Correct a line, or price one that is waiting for a price."""

    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=_POSITIVE, required=False
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False
    )

    class Meta:
        model = InvoiceLine
        fields = ["description", "quantity", "unit_price"]


class InvoiceManualLineSerializer(serializers.Serializer[Any]):
    """A line the generator could not know about."""

    description = serializers.CharField(max_length=500)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=_POSITIVE)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))


class InvoiceIssueSerializer(serializers.Serializer[Any]):
    invoice_number = serializers.CharField(max_length=40)
    invoice_date = serializers.DateField()


class InvoiceMarkPaidSerializer(serializers.Serializer[Any]):
    paid_on = serializers.DateField(help_text="The collection's date.")
    # Scoped in get_fields: the firm's active cash accounts.
    cash_account = serializers.PrimaryKeyRelatedField(
        queryset=CashAccount.objects.none(),
        required=False,
        allow_null=True,
        help_text="Where the money went. The firm's main till when omitted.",
    )
    method = serializers.ChoiceField(
        choices=PaymentMethod.choices, default=PaymentMethod.TRANSFER, required=False
    )

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        till = cast(serializers.PrimaryKeyRelatedField[CashAccount], fields["cash_account"])
        till.queryset = CashAccount.objects.filter(is_active=True)
        return fields


class InvoiceCancelSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=1000)
