from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)


class StrictMixin:
    """Rejects a field the client sent that this serializer does not declare.

    Copied rather than imported - every app that needs this defines its own
    (see apps/contracts, apps/customers, apps/elevators, apps/properties);
    there is no shared core.serializers module to pull it from.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


class MaintenanceVisitItemSerializer(serializers.ModelSerializer[MaintenanceVisitItem]):
    class Meta:
        model = MaintenanceVisitItem
        fields = ["id", "item", "result", "note"]
        read_only_fields = ["id"]


class MaintenanceVisitReadSerializer(serializers.ModelSerializer[MaintenanceVisit]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    technician_name = serializers.SerializerMethodField()
    items = MaintenanceVisitItemSerializer(many=True, read_only=True)

    class Meta:
        model = MaintenanceVisit
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "contract",
            "technician",
            "technician_name",
            "visit_date",
            "status",
            "status_note",
            "work_notes",
            "parts_replaced",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_technician_name(self, visit: MaintenanceVisit) -> str:
        user = visit.technician
        return f"{user.first_name} {user.last_name}".strip()


class MaintenanceVisitWriteSerializer(StrictMixin, serializers.ModelSerializer[MaintenanceVisit]):
    status = serializers.ChoiceField(choices=VisitStatus.choices)

    class Meta:
        model = MaintenanceVisit
        fields = [
            "elevator",
            "contract",
            "technician",
            "visit_date",
            "status",
            "status_note",
            "work_notes",
            "parts_replaced",
        ]


class SetChecklistItemSerializer(serializers.Serializer[Any]):
    item = serializers.ChoiceField(choices=ChecklistItem.choices)
    result = serializers.ChoiceField(choices=ChecklistResult.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class SetChecklistSerializer(serializers.Serializer[Any]):
    items = SetChecklistItemSerializer(many=True)

    def validate_items(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        seen = {entry["item"] for entry in items}
        if len(seen) != len(items):
            raise serializers.ValidationError("Each checklist item may appear at most once.")
        return items
