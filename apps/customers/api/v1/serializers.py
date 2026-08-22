from __future__ import annotations

from typing import Any

from django.db import transaction
from rest_framework import serializers

from apps.customers.models import ContactRole, Customer, CustomerContact, CustomerType
from apps.customers.services import demote_other_primaries
from core.crypto import fingerprint
from core.error_codes import ErrorCode
from core.exceptions import DuplicateRecord
from core.validators import (
    normalize_email,
    normalize_phone,
    validate_national_id,
    validate_tax_number,
)

#: Everything that is not a person. The identifier that belongs to a customer
#: follows from this: an organisation is billed against a tax number, a person
#: against a national ID, and carrying the other one means the record will be
#: invoiced under an identity that is not theirs.
ORGANISATION_TYPES = frozenset(CustomerType.values) - {CustomerType.INDIVIDUAL}


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

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> CustomerContact:
        # Before the insert, not after: the constraint fires on the INSERT
        # itself, so demoting afterwards never gets a turn.
        if validated_data.get("is_primary"):
            demote_other_primaries(validated_data["customer"])
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance: CustomerContact, validated_data: dict[str, Any]) -> CustomerContact:
        if validated_data.get("is_primary") and not instance.is_primary:
            demote_other_primaries(instance.customer, keep_pk=instance.pk)
        return super().update(instance, validated_data)


class CustomerContactNestedWriteSerializer(CustomerContactWriteSerializer):
    """The same contact, created under `/customers/{id}/contacts`.

    `customer` is gone from the field list rather than made read-only, so
    `StrictMixin` refuses a body that names it. Two sources for one value is how
    they come to disagree, and the path is the source.
    """

    class Meta(CustomerContactWriteSerializer.Meta):
        fields = ["full_name", "role", "phone", "email", "is_primary", "notes"]


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

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        self._check_type_rules(attrs)
        self._check_identifiers_are_free(attrs)
        return attrs

    def _effective(self, attrs: dict[str, Any], field: str) -> str:
        """What the field will hold once this request is applied.

        A PATCH carries only what changed, so a rule that read `attrs` alone
        would clear an individual's national ID by accepting a request that
        never mentioned it.
        """
        if field in attrs:
            return attrs[field] or ""
        return getattr(self.instance, field, "") or ""

    def _check_type_rules(self, attrs: dict[str, Any]) -> None:
        customer_type = attrs.get("type") or getattr(self.instance, "type", None)
        if customer_type == CustomerType.INDIVIDUAL:
            forbidden, required = ("tax_number", "tax_office"), "national_id"
        elif customer_type in ORGANISATION_TYPES:
            forbidden, required = ("national_id",), "tax_number"
        else:
            # No type at all; the field validator has already said so.
            return

        errors: dict[str, list[serializers.ErrorDetail]] = {}
        for field in forbidden:
            if self._effective(attrs, field):
                errors[field] = [_reason(ErrorCode.FIELD_NOT_VALID_FOR_CUSTOMER_TYPE)]
        if self._must_be_present(attrs, required) and not self._effective(attrs, required):
            errors[required] = [_reason(ErrorCode.FIELD_REQUIRED_FOR_CUSTOMER_TYPE)]
        if errors:
            raise serializers.ValidationError(errors)

    def _must_be_present(self, attrs: dict[str, Any], field: str) -> bool:
        """Whether this request has to satisfy the requirement, or may leave it.

        Always on create, and on any update that touches the field or changes
        the type — so a request can never store a record without its identifier,
        clear one, or move a record to a type whose identifier it lacks.

        What it deliberately allows is editing one unrelated field of a record
        that predates the rule. Refusing that would leave those records frozen:
        every edit would fail, including the edit that would have completed
        them. The form sends the whole set, so the next real edit still asks.
        """
        if self.instance is None:
            return True
        return field in attrs or ("type" in attrs and attrs["type"] != self.instance.type)

    def _check_identifiers_are_free(self, attrs: dict[str, Any]) -> None:
        """Refuse a number this company has already given to somebody else.

        The constraints in the schema are the guarantee; this is what turns the
        guarantee into an answer that names which number was the problem. The
        manager is already scoped to the company and to live rows, so a
        soft-deleted customer's number is free again.
        """
        tax_number = self._effective(attrs, "tax_number")
        if tax_number:
            self._refuse_if_taken({"tax_number": tax_number}, ErrorCode.DUPLICATE_TAX_NUMBER)

        national_id = self._effective(attrs, "national_id")
        if national_id:
            # Through the blind index: the ciphertext differs on every write, so
            # the column itself cannot be compared.
            self._refuse_if_taken(
                {"national_id_fingerprint": fingerprint(national_id)},
                ErrorCode.DUPLICATE_NATIONAL_ID,
            )

    def _refuse_if_taken(self, lookup: dict[str, str], code: ErrorCode) -> None:
        taken = Customer.objects.filter(**lookup)
        if self.instance is not None:
            # Saving a record without changing its number is not a collision
            # with itself.
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise DuplicateRecord(code)


def _reason(code: ErrorCode) -> serializers.ErrorDetail:
    """A field error the client can switch on rather than read."""
    return serializers.ErrorDetail(code.label, code=code.value)
