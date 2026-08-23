from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.properties.models import Building, BuildingType, Complex
from core.error_codes import ErrorCode
from core.serializers import AddressReadMixin


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


class ComplexReadSerializer(AddressReadMixin, serializers.ModelSerializer[Complex]):
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    building_count = serializers.IntegerField(read_only=True, default=0)
    elevator_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Complex
        fields = [
            "id",
            "name",
            "customer_id",
            "customer_name",
            "neighborhood_id",
            "neighborhood_name",
            "district_name",
            "province_name",
            "street",
            "building_number",
            "latitude",
            "longitude",
            "notes",
            "building_count",
            "elevator_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ComplexWriteSerializer(StrictMixin, serializers.ModelSerializer[Complex]):
    class Meta:
        model = Complex
        fields = [
            "customer",
            "name",
            "neighborhood",
            "street",
            "building_number",
            "latitude",
            "longitude",
            "notes",
        ]


class BuildingReadSerializer(AddressReadMixin, serializers.ModelSerializer[Building]):
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    complex_name = serializers.CharField(source="complex.name", read_only=True, default=None)
    elevator_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Building
        fields = [
            "id",
            "name",
            "type",
            "customer_id",
            "customer_name",
            "complex_id",
            "complex_name",
            "neighborhood_id",
            "neighborhood_name",
            "district_name",
            "province_name",
            "street",
            "building_number",
            "address_note",
            "latitude",
            "longitude",
            "floor_count",
            "unit_count",
            "is_active",
            "elevator_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class BuildingWriteSerializer(StrictMixin, serializers.ModelSerializer[Building]):
    type = serializers.ChoiceField(choices=BuildingType.choices)

    class Meta:
        model = Building
        fields = [
            "customer",
            "complex",
            "name",
            "type",
            "neighborhood",
            "street",
            "building_number",
            "address_note",
            "latitude",
            "longitude",
            "floor_count",
            "unit_count",
            "is_active",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)

        complex_obj = attrs.get("complex", getattr(self.instance, "complex", None))
        customer = attrs.get("customer", getattr(self.instance, "customer", None))

        # A complex belongs to one customer. Letting a building under it name a
        # different one would put the contract and the invoice on two different
        # parties for the same address.
        if (
            complex_obj is not None
            and customer is not None
            and complex_obj.customer_id != customer.id
        ):
            raise serializers.ValidationError(
                {"complex": ErrorCode.BUILDING_CUSTOMER_MISMATCH.value}
            )
        return attrs
