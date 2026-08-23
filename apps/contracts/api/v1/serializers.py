from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.contracts.models import (
    BillingPeriod,
    Contract,
    ContractElevator,
    ContractStatus,
    PricingType,
    Scope,
    VatStatus,
)
from core.error_codes import ErrorCode

# The financial fields, named once. The viewset drops them for roles that do
# not carry money, and the same list decides what a serializer omits — so the
# two can never disagree.
FINANCIAL_FIELDS = (
    "pricing_type",
    "monthly_fee",
    "currency",
    "vat_rate",
    "vat_status",
    "billing_period",
    "monthly_subtotal",
    "vat_amount",
    "monthly_total",
)


class StrictMixin:
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        # The mixin is declared standalone, so `super()` is only a serializer
        # once it is mixed in; the ignore is that, and the local annotation is
        # what stops the resulting Any leaking into every caller.
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


class ContractLineSerializer(serializers.ModelSerializer[ContractElevator]):
    registration_number = serializers.CharField(
        source="elevator.registration_number", read_only=True
    )
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    building_name = serializers.CharField(source="elevator.building.name", read_only=True)

    class Meta:
        model = ContractElevator
        fields = [
            "id",
            "elevator_id",
            "registration_number",
            "elevator_name",
            "building_name",
            "unit_price",
            "added_at",
            "removed_at",
        ]
        read_only_fields = fields


class ContractReadSerializer(serializers.ModelSerializer[Contract]):
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    lines = ContractLineSerializer(many=True, read_only=True)
    elevator_count = serializers.IntegerField(read_only=True, default=0)
    previous_contract_number = serializers.CharField(
        source="previous_contract.contract_number", read_only=True, default=""
    )
    monthly_subtotal = serializers.SerializerMethodField()
    vat_status = serializers.SerializerMethodField()
    vat_amount = serializers.SerializerMethodField()
    monthly_total = serializers.SerializerMethodField()

    def _subtotal(self, contract: Contract) -> Decimal:
        """What the customer is billed each month, before VAT.

        A flat contract is its monthly fee. A per-elevator contract is the sum
        over its *open* lines — a line closed by a termination or a removal is
        no longer billed, and including it would keep charging for a lift that
        left the agreement.

        An open line with no price counts as zero rather than being skipped.
        That is usually somebody adding an elevator and forgetting to price it,
        and a total that quietly matches the priced ones hides it.
        """
        if contract.pricing_type == PricingType.FLAT:
            return contract.monthly_fee or Decimal("0")
        return sum(
            (line.unit_price or Decimal("0"))
            for line in contract.lines.all()
            if line.removed_at is None
        ) or Decimal("0")

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_monthly_subtotal(self, contract: Contract) -> str:
        return f"{self._subtotal(contract):.2f}"

    @extend_schema_field(serializers.ChoiceField(choices=VatStatus.choices))
    def get_vat_status(self, contract: Contract) -> str:
        """What a screen switches on to explain the two fields below.

        `vat_rate` being `null` already distinguishes the cases in principle,
        but only to a reader who knows that a null there is not a zero. This
        names it, so the hint beside the total is read off the response instead
        of being inferred — and the name is a code, not a sentence, because the
        Turkish for it lives in the frontend's translation file.
        """
        return contract.vat_status

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_vat_amount(self, contract: Contract) -> str | None:
        rate = contract.vat_rate
        if rate is None:
            # Null, not "0.00". Nobody stated a rate, so there is no VAT figure
            # to state — and a zero here is indistinguishable from a contract
            # that really is zero-rated, which is a different fact entirely.
            return None
        # Rounded once, here, rather than per line. Rounding each line and then
        # adding them drifts by a hundredth per line, which is exactly the kind of
        # difference an accountant notices and nobody can explain.
        amount = (self._subtotal(contract) * rate / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return f"{amount:.2f}"

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_monthly_total(self, contract: Contract) -> str | None:
        vat = self.get_vat_amount(contract)
        if vat is None:
            # It refuses rather than computing the subtotal again under another
            # name. A number here is short by the VAT and looks finished; that
            # is the whole failure, and it only shows up at reconciliation
            # months later, across every invoice raised from this contract.
            # `monthly_subtotal` is still answered — that part is known.
            return None
        total = self._subtotal(contract) + Decimal(vat)
        return f"{total:.2f}"

    class Meta:
        model = Contract
        fields = [
            "id",
            "contract_number",
            "customer_id",
            "customer_name",
            "status",
            "scope",
            "start_date",
            "end_date",
            "pricing_type",
            "monthly_fee",
            "currency",
            "vat_rate",
            "billing_period",
            "auto_renew",
            "renewal_notice_days",
            "previous_contract_id",
            "terminated_at",
            "termination_reason",
            "notes",
            "elevator_count",
            "previous_contract_number",
            # Computed on the server because money does not cross this wire as a
            # float. Adding the lines up in a browser parses those strings back
            # into floats, and a contract worth 4,750.00 renders as
            # 4,749.999999999999.
            "monthly_subtotal",
            # `vat_amount` and `monthly_total` are null when no rate was ever
            # stated. `vat_status` says which of the three cases that is.
            "vat_status",
            "vat_amount",
            "monthly_total",
            "lines",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance: Contract) -> dict[str, Any]:
        data = super().to_representation(instance)
        # Operations runs the fleet, not the money. The fields are removed from
        # the payload rather than blanked: a null would read as "not set yet"
        # and send someone looking for a value that is simply not theirs.
        if not self.context.get("show_financials", True):
            for field in FINANCIAL_FIELDS:
                data.pop(field, None)
            for line in data.get("lines", []):
                line.pop("unit_price", None)
        return data


class ContractWriteSerializer(StrictMixin, serializers.ModelSerializer[Contract]):
    status = serializers.ChoiceField(choices=ContractStatus.choices, required=False)
    scope = serializers.ChoiceField(choices=Scope.choices)
    pricing_type = serializers.ChoiceField(choices=PricingType.choices)
    billing_period = serializers.ChoiceField(choices=BillingPeriod.choices, required=False)
    # Required on create, and never nullable. Elevator maintenance is
    # VAT-liable in Turkey, so a blank rate is an omission far more often than
    # a decision — and the decision has its own way of saying itself: 0.00.
    # This is the one place a person leaves the field empty, so it is the one
    # place worth closing.
    #
    # PATCH stays partial, so an existing contract can have its notes edited
    # without restating its rate; `allow_null=False` means it cannot have the
    # rate cleared either. Bounded here as well as in the database so the
    # answer is a 400 on a named field rather than an integrity error.
    vat_rate = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        allow_null=False,
    )

    class Meta:
        model = Contract
        fields = [
            "customer",
            "contract_number",
            "status",
            "scope",
            "start_date",
            "end_date",
            "pricing_type",
            "monthly_fee",
            "currency",
            "vat_rate",
            "billing_period",
            "auto_renew",
            "renewal_notice_days",
            "notes",
        ]
        extra_kwargs = {"contract_number": {"required": False}}

    def validate_status(self, value: str) -> str:
        # Both of these are reached through their own endpoint, because each has
        # side effects across three tables. Allowing them here would make those
        # effects the client's responsibility.
        if value in (ContractStatus.TERMINATED, ContractStatus.RENEWED):
            raise serializers.ValidationError(code=ErrorCode.STATUS_NOT_USER_SELECTABLE)
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"end_date": ErrorCode.END_DATE_BEFORE_START_DATE.value}
            )
        return attrs


class AddElevatorsSerializer(StrictMixin, serializers.Serializer[Any]):
    elevator_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )


class TerminateSerializer(StrictMixin, serializers.Serializer[Any]):
    terminated_at = serializers.DateField()
    # allow_blank so DRF's generic "blank" error does not fire first and hide
    # the specific code the client needs to translate.
    reason = serializers.CharField(max_length=2000, allow_blank=True)

    def validate_reason(self, value: str) -> str:
        # Caught here rather than in the service so the client gets a field to
        # highlight, and gets it in the same vocabulary the service would have
        # used. The service keeps its own check as a backstop for callers that
        # do not come through the API.
        if not value.strip():
            raise serializers.ValidationError(code=ErrorCode.TERMINATION_REASON_REQUIRED)
        return value


class RenewSerializer(StrictMixin, serializers.Serializer[Any]):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    carry_elevators = serializers.BooleanField(default=True)
    copy_terms = serializers.BooleanField(default=True)
