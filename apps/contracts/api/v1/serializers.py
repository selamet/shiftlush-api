from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.contracts.models import (
    BillingPeriod,
    Contract,
    ContractElevator,
    ContractStatus,
    PricingType,
    Scope,
)
from core.error_codes import ErrorCode

# The financial fields, named once. The viewset drops them for roles that do
# not carry money, and the same list decides what a serializer omits — so the
# two can never disagree.
FINANCIAL_FIELDS = ("pricing_type", "monthly_fee", "currency", "vat_rate", "billing_period")


class StrictMixin:
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        return super().validate(attrs)  # type: ignore[misc]


class ContractLineSerializer(serializers.ModelSerializer):
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


class ContractReadSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    lines = ContractLineSerializer(many=True, read_only=True)
    elevator_count = serializers.IntegerField(read_only=True, default=0)

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


class ContractWriteSerializer(StrictMixin, serializers.ModelSerializer):
    status = serializers.ChoiceField(choices=ContractStatus.choices, required=False)
    scope = serializers.ChoiceField(choices=Scope.choices)
    pricing_type = serializers.ChoiceField(choices=PricingType.choices)
    billing_period = serializers.ChoiceField(choices=BillingPeriod.choices, required=False)

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


class AddElevatorsSerializer(StrictMixin, serializers.Serializer):
    elevator_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )


class TerminateSerializer(StrictMixin, serializers.Serializer):
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


class RenewSerializer(StrictMixin, serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    carry_elevators = serializers.BooleanField(default=True)
    copy_terms = serializers.BooleanField(default=True)
