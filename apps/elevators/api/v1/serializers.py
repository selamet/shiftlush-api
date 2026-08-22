from __future__ import annotations

from datetime import date
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.elevators.labels import MAX_LABELS
from apps.elevators.models import (
    USER_SELECTABLE_STATUSES,
    Category,
    ControlType,
    DoorType,
    DriveType,
    Elevator,
    ElevatorStatus,
    InspectionLabel,
    MachineRoom,
)
from core.error_codes import ErrorCode
from core.permissions import READ, may


class StrictMixin:
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        return super().validate(attrs)  # type: ignore[misc]


class ElevatorListSerializer(serializers.ModelSerializer):
    """What the list screen needs — seven columns' worth, not all 31 fields.

    The rest are record fields rather than search criteria and live on the
    detail response; sending them all would make the heaviest screen in the
    product heavier for nothing.
    """

    building_name = serializers.CharField(source="building.name", read_only=True)
    customer_name = serializers.CharField(source="building.customer.legal_name", read_only=True)

    class Meta:
        model = Elevator
        fields = [
            "id",
            "registration_number",
            "name",
            "category",
            "stop_count",
            "has_car_door",
            "building_id",
            "building_name",
            "customer_name",
            "status",
            "inspection_label",
            "next_inspection_date",
            "brand",
            "model",
        ]
        read_only_fields = fields


class CurrentContractSerializer(serializers.Serializer):
    """The contract covering this elevator right now, if any.

    A compact block rather than the whole contract: the detail screen shows four
    facts, and embedding a full contract here would drag its lines — every other
    elevator in the same agreement — into a response about one lift.
    """

    id = serializers.UUIDField(read_only=True)
    contract_number = serializers.CharField(read_only=True)
    scope = serializers.CharField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)


class ElevatorDetailSerializer(serializers.ModelSerializer):
    building_name = serializers.CharField(source="building.name", read_only=True)
    customer_id = serializers.UUIDField(source="building.customer_id", read_only=True)
    customer_name = serializers.CharField(source="building.customer.legal_name", read_only=True)
    complex_name = serializers.CharField(
        source="building.complex.name", read_only=True, default=None
    )
    current_contract = serializers.SerializerMethodField()

    @extend_schema_field(CurrentContractSerializer(allow_null=True))
    def get_current_contract(self, elevator: Elevator) -> dict[str, Any] | None:
        """The elevator's open contract line, if it has one.

        "Open line" is the domain's own definition of cover — it is what
        `add_elevators` checks before refusing a second contract, and what the
        database constraint enforces. Terminating a contract closes its lines,
        so a second condition on contract status would only add a way for the
        two rules to disagree. A draft contract counts, which matches the fact
        that adding an elevator to one already marks the elevator active.
        """
        line = next(
            (
                candidate
                for candidate in elevator.contract_lines.all()
                if candidate.removed_at is None
            ),
            None,
        )
        if line is None:
            return None

        # Through the serializer rather than returned raw. A SerializerMethodField
        # hands its dict straight to the renderer, so a raw Decimal would reach
        # the client as a JSON *number* — and money crossing the wire as a float
        # is the one thing this API does not do.
        block = CurrentContractSerializer(
            {
                "id": line.contract.id,
                "contract_number": line.contract.contract_number,
                "scope": line.contract.scope,
                "end_date": line.contract.end_date,
                "unit_price": line.unit_price,
            }
        ).data
        # Dropped rather than nulled for roles that may not see money. `null`
        # says the contract has no price on this line; absence says the reader
        # is not entitled to ask. Hiding it in the client instead would be
        # decoration — the value would still be in the response body.
        request = self.context.get("request")
        role = getattr(getattr(request, "user", None), "role", None)
        if not may(role, "contract_financials", READ):
            block.pop("unit_price", None)
        return block

    class Meta:
        model = Elevator
        fields = [
            "id",
            "building_id",
            "building_name",
            "customer_id",
            "customer_name",
            "complex_name",
            "current_contract",
            "registration_number",
            "internal_code",
            "name",
            "qr_token",
            "qr_token_generated_at",
            "category",
            "drive_type",
            "control_type",
            "door_type",
            "has_car_door",
            "machine_room",
            "capacity_kg",
            "capacity_persons",
            "stop_count",
            "entrance_count",
            "speed_mps",
            "pit_depth_mm",
            "headroom_mm",
            "car_weight_kg",
            "brand",
            "model",
            "serial_number",
            "manufacturer",
            "installer",
            "installation_date",
            "commissioning_date",
            "ce_certificate_number",
            "warranty_end_date",
            "last_inspection_date",
            "inspection_label",
            "next_inspection_date",
            "inspection_body",
            "inspection_report_number",
            "status",
            "maintenance_interval_days",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ElevatorWriteSerializer(StrictMixin, serializers.ModelSerializer):
    category = serializers.ChoiceField(choices=Category.choices, required=False, allow_blank=True)
    drive_type = serializers.ChoiceField(
        choices=DriveType.choices, required=False, allow_blank=True
    )
    control_type = serializers.ChoiceField(
        choices=ControlType.choices, required=False, allow_blank=True
    )
    door_type = serializers.ChoiceField(choices=DoorType.choices, required=False, allow_blank=True)
    machine_room = serializers.ChoiceField(
        choices=MachineRoom.choices, required=False, allow_blank=True
    )
    inspection_label = serializers.ChoiceField(
        choices=InspectionLabel.choices, required=False, default=InspectionLabel.NONE
    )
    # Only the four a user can actually choose. `uncontracted` is assigned by
    # the contract service and offering it here would let a client detach an
    # elevator from its contract by editing a dropdown.
    status = serializers.ChoiceField(
        choices=[(value, value) for value in USER_SELECTABLE_STATUSES], required=False
    )

    class Meta:
        model = Elevator
        fields = [
            "building",
            "registration_number",
            "internal_code",
            "name",
            "category",
            "drive_type",
            "control_type",
            "door_type",
            "has_car_door",
            "machine_room",
            "capacity_kg",
            "capacity_persons",
            "stop_count",
            "entrance_count",
            "speed_mps",
            "pit_depth_mm",
            "headroom_mm",
            "car_weight_kg",
            "brand",
            "model",
            "serial_number",
            "manufacturer",
            "installer",
            "installation_date",
            "commissioning_date",
            "ce_certificate_number",
            "warranty_end_date",
            "last_inspection_date",
            "inspection_label",
            "next_inspection_date",
            "inspection_body",
            "inspection_report_number",
            "status",
            "maintenance_interval_days",
            "notes",
        ]

    def validate_installation_date(self, value: date | None) -> date | None:
        if value and value > date.today():
            raise serializers.ValidationError(code=ErrorCode.INSTALLATION_DATE_IN_FUTURE)
        return value

    def validate_status(self, value: str) -> str:
        if value == ElevatorStatus.UNCONTRACTED:
            raise serializers.ValidationError(code=ErrorCode.STATUS_NOT_USER_SELECTABLE)
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)

        last = attrs.get(
            "last_inspection_date", getattr(self.instance, "last_inspection_date", None)
        )
        nxt = attrs.get(
            "next_inspection_date", getattr(self.instance, "next_inspection_date", None)
        )
        if last and nxt and nxt <= last:
            raise serializers.ValidationError(
                {"next_inspection_date": ErrorCode.END_DATE_BEFORE_START_DATE.value}
            )
        return attrs


class ElevatorByQrSerializer(serializers.ModelSerializer):
    """The six values that decide what happens next on site.

    Deliberately narrow: this is what a technician sees after scanning, on a
    phone, in a machine room. The full record is one tap away.
    """

    building_name = serializers.CharField(source="building.name", read_only=True)
    customer_name = serializers.CharField(source="building.customer.legal_name", read_only=True)

    class Meta:
        model = Elevator
        fields = [
            "id",
            "registration_number",
            "name",
            "building_name",
            "customer_name",
            "status",
            "inspection_label",
            "has_car_door",
            "brand",
            "model",
            "capacity_kg",
            "capacity_persons",
            "stop_count",
            "speed_mps",
            "last_inspection_date",
            "next_inspection_date",
        ]
        read_only_fields = fields


class LabelRequestSerializer(serializers.Serializer):
    """Which elevators to print, in the order they should appear.

    A list rather than a filter: the user ticks rows on a screen, and a filter
    re-evaluated on the server could quietly print a different set from the one
    they were looking at.
    """

    elevator_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        # A firm with five hundred lifts pressing "print all" would otherwise
        # hold a worker for a minute and produce a document nobody prints in
        # one sitting.
        max_length=MAX_LABELS,
    )
