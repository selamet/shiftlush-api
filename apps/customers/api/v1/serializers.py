from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.customers.models import ContactRole, Customer, CustomerContact, CustomerType
from core.validators import (
    normalize_email,
    normalize_phone,
    validate_national_id,
    validate_tax_number,
)


class StrictMixin:
    """Rejects fields the serializer does not declare.

    DRF ignores them, so a mistyped key returns 200 and the value silently
    never arrives.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        return super().validate(attrs)  # type: ignore[misc]


class CustomerContactReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerContact
        fields = [
            "id",
            "customer_id",
            "full_name",
            "role",
            "phone",
            "email",
            "is_primary",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class CustomerContactWriteSerializer(StrictMixin, serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=ContactRole.choices, default=ContactRole.OTHER)

    class Meta:
        model = CustomerContact
        # Listed one by one. `__all__` would put `company`, `is_deleted` and
        # every future column on the write surface.
        fields = ["customer", "full_name", "role", "phone", "email", "is_primary", "notes"]

    def validate_phone(self, value: str) -> str:
        return normalize_phone(value) if value else value

    def validate_email(self, value: str) -> str:
        return normalize_email(value) if value else value


class CustomerReadSerializer(serializers.ModelSerializer):
    contacts = CustomerContactReadSerializer(many=True, read_only=True)
    building_count = serializers.IntegerField(read_only=True, default=0)
    elevator_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Customer
        fields = [
            "id",
            "type",
            "legal_name",
            "tax_office",
            "tax_number",
            "phone",
            "email",
            "neighborhood_id",
            "street",
            "building_number",
            "unit_number",
            "notes",
            "is_active",
            "contacts",
            "building_count",
            "elevator_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
        # national_id is absent on purpose. It is personal data, it is encrypted
        # at rest, and no list or detail screen needs it — putting it in the
        # default response would leak it to every client that renders a table.


class CustomerWriteSerializer(StrictMixin, serializers.ModelSerializer):
    type = serializers.ChoiceField(choices=CustomerType.choices)

    class Meta:
        model = Customer
        fields = [
            "type",
            "legal_name",
            "tax_office",
            "tax_number",
            "national_id",
            "phone",
            "email",
            "neighborhood",
            "street",
            "building_number",
            "unit_number",
            "notes",
            "is_active",
        ]
        extra_kwargs = {"national_id": {"write_only": True}}

    def validate_tax_number(self, value: str) -> str:
        return validate_tax_number(value) if value else value

    def validate_national_id(self, value: str) -> str:
        return validate_national_id(value) if value else value

    def validate_phone(self, value: str) -> str:
        return normalize_phone(value) if value else value

    def validate_email(self, value: str) -> str:
        return normalize_email(value) if value else value
