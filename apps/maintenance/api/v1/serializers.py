from __future__ import annotations

from typing import Any, cast

from rest_framework import serializers

from apps.maintenance import services
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)
from apps.users.models import Role, User
from core.error_codes import ErrorCode
from core.requests import authenticated_user


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


def _each_item_once(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = {entry["item"] for entry in items}
    if len(seen) != len(items):
        raise serializers.ValidationError("Each checklist item may appear at most once.")
    return items


class TechnicianOptionSerializer(serializers.Serializer[Any]):
    """One choice in the "who did the work" field — a name and nothing else."""

    id = serializers.UUIDField()
    name = serializers.CharField()


class SetChecklistItemSerializer(serializers.Serializer[Any]):
    item = serializers.ChoiceField(choices=ChecklistItem.choices)
    result = serializers.ChoiceField(choices=ChecklistResult.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class SetChecklistSerializer(serializers.Serializer[Any]):
    items = SetChecklistItemSerializer(many=True)

    def validate_items(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        return _each_item_once(items)


class MaintenanceVisitWriteSerializer(StrictMixin, serializers.ModelSerializer[MaintenanceVisit]):
    status = serializers.ChoiceField(choices=VisitStatus.choices)
    # Scoped in get_fields below. Not required here because a technician may
    # leave it out; validate() requires it from everybody else.
    technician = serializers.PrimaryKeyRelatedField(queryset=User.objects.none(), required=False)
    # The checklist, in the same request as the visit, so a phone sending from
    # its outbox records the whole form or none of it. Sending it replaces the
    # visit's checklist; leaving it out leaves the checklist alone.
    items = SetChecklistItemSerializer(many=True, required=False)

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
            "items",
        ]

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        # User is the one model whose default manager is not tenant-scoped, so
        # the elevator and contract fields are safe by default and this one is
        # not: without this, a visit could name a user from another firm and
        # its response would read back that user's name. Someone outside the
        # firm, or no longer active in it, is "does not exist" — the same
        # answer as an id that was never issued.
        #
        # With no signed-in user — schema generation, a serializer built by
        # hand — the declared `none()` stands: nobody can be named.
        user = getattr(self.context.get("request"), "user", None)
        company_id = getattr(user, "company_id", None)
        if company_id is not None:
            technician = cast(serializers.PrimaryKeyRelatedField[User], fields["technician"])
            technician.queryset = User.objects.filter(company_id=company_id, is_active=True)
        return fields

    def validate_items(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        return _each_item_once(items)

    def create(self, validated_data: dict[str, Any]) -> MaintenanceVisit:
        items = validated_data.pop("items", None)
        visit = super().create(validated_data)
        if items is not None:
            services.set_checklist(visit=visit, items=items)
        return visit

    def update(
        self, instance: MaintenanceVisit, validated_data: dict[str, Any]
    ) -> MaintenanceVisit:
        items = validated_data.pop("items", None)
        visit = super().update(instance, validated_data)
        if items is not None:
            services.set_checklist(visit=visit, items=items)
        return visit

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        user = authenticated_user(self.context["request"])
        named = attrs.get("technician")

        if user.role == Role.TECHNICIAN:
            # A visit is a statement that the named person did the work. A
            # technician makes that statement about themselves only, and does
            # not have to repeat their own name to do it.
            if named is not None and named.pk != user.pk:
                code = ErrorCode.TECHNICIAN_RECORDS_OWN_VISITS
                raise serializers.ValidationError(
                    {"technician": serializers.ErrorDetail(code.label, code=code.value)}
                )
            if self.instance is None:
                attrs["technician"] = user
        elif named is None and self.instance is None:
            raise serializers.ValidationError(
                {"technician": serializers.ErrorDetail("This field is required.", code="required")}
            )
        return attrs
