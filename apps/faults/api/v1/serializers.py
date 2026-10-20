from __future__ import annotations

from typing import Any, cast

from rest_framework import serializers

from apps.elevators.models import Elevator
from apps.faults.models import Fault, FaultPriority
from apps.users.models import Role, User
from core.requests import authenticated_user


class StrictMixin:
    """Rejects a field the client sent that this serializer does not declare.

    Copied rather than imported, as in every other app (see
    apps/maintenance/api/v1/serializers.py).
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


def _firm_users(serializer: serializers.Serializer[Any]) -> Any:
    """Active users of the signed-in user's firm, or nobody.

    SL-124's rule for every user field: User is the one model whose manager is
    not tenant-scoped, so without this a fault could name somebody from another
    firm. With no signed-in user — schema generation — nobody can be named.
    """
    user = getattr(serializer.context.get("request"), "user", None)
    company_id = getattr(user, "company_id", None)
    if company_id is None:
        return User.objects.none()
    return User.objects.filter(company_id=company_id, is_active=True)


def _seconds(value: Any) -> int | None:
    return None if value is None else int(value.total_seconds())


class FaultReadSerializer(serializers.ModelSerializer[Fault]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    building = serializers.UUIDField(source="elevator.building_id", read_only=True)
    building_name = serializers.CharField(source="elevator.building.name", read_only=True)
    customer = serializers.UUIDField(source="elevator.building.customer_id", read_only=True)
    customer_name = serializers.CharField(
        source="elevator.building.customer.legal_name", read_only=True
    )
    reported_by_name = serializers.SerializerMethodField()
    technician_name = serializers.SerializerMethodField()
    additional_reports = serializers.ListField(child=serializers.DictField(), read_only=True)
    response_time_seconds = serializers.SerializerMethodField(
        help_text="Seconds from the report to the technician arriving; null until they arrive."
    )
    resolution_time_seconds = serializers.SerializerMethodField(
        help_text="Seconds from the report to its resolution; null until resolved."
    )

    class Meta:
        model = Fault
        fields = [
            "id",
            "reference",
            "elevator",
            "elevator_name",
            "building",
            "building_name",
            "customer",
            "customer_name",
            "channel",
            "reported_by",
            "reported_by_name",
            "reporter_name",
            "reporter_phone",
            "description",
            "additional_reports",
            "priority",
            "status",
            "technician",
            "technician_name",
            "stopped_elevator",
            "reported_at",
            "assigned_at",
            "on_site_at",
            "resolved_at",
            "closed_at",
            "cancelled_at",
            "cancel_reason",
            "resolution_notes",
            "parts_used",
            "billable",
            "billable_overridden",
            "response_time_seconds",
            "resolution_time_seconds",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_reported_by_name(self, fault: Fault) -> str | None:
        return fault.reported_by.full_name if fault.reported_by else None

    def get_technician_name(self, fault: Fault) -> str | None:
        return fault.technician.full_name if fault.technician else None

    def get_response_time_seconds(self, fault: Fault) -> int | None:
        return _seconds(fault.response_time)

    def get_resolution_time_seconds(self, fault: Fault) -> int | None:
        return _seconds(fault.resolution_time)


class FaultCreateSerializer(StrictMixin, serializers.ModelSerializer[Fault]):
    """A fault entered by somebody at the firm.

    The state, the channel and every timestamp are the server's. A trapped
    passenger makes it an emergency whatever `priority` says, so a phone form
    in a hurry cannot under-state one.
    """

    # Scoped in get_fields: the firm's lifts, and for a technician only the
    # lifts of the customers they are assigned to.
    elevator = serializers.PrimaryKeyRelatedField(queryset=Elevator.objects.none())
    technician = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.none(), required=False, allow_null=True
    )
    priority = serializers.ChoiceField(
        choices=FaultPriority.choices, default=FaultPriority.NORMAL, required=False
    )
    someone_trapped = serializers.BooleanField(default=False, required=False, write_only=True)

    class Meta:
        model = Fault
        fields = [
            "elevator",
            "description",
            "priority",
            "technician",
            "stopped_elevator",
            "someone_trapped",
        ]

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        technician = cast(serializers.PrimaryKeyRelatedField[User], fields["technician"])
        technician.queryset = _firm_users(self)
        elevator = cast(serializers.PrimaryKeyRelatedField[Elevator], fields["elevator"])
        user = getattr(self.context.get("request"), "user", None)
        if isinstance(user, User):
            lifts = Elevator.objects.all()
            if user.role == Role.TECHNICIAN:
                assigned = user.customer_assignments.values_list("customer_id", flat=True)
                lifts = lifts.filter(building__customer_id__in=list(assigned))
            elevator.queryset = lifts
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        user = authenticated_user(self.context["request"])
        named = attrs.get("technician")
        # Sending somebody is the office's decision; a technician who reports
        # a fault may take it themselves, and nobody else.
        if user.role == Role.TECHNICIAN and named is not None and named.pk != user.pk:
            raise serializers.ValidationError(
                {
                    "technician": serializers.ErrorDetail(
                        "A technician may name only themselves.", "invalid"
                    )
                }
            )
        return attrs


class FaultEditSerializer(StrictMixin, serializers.ModelSerializer[Fault]):
    """The office's corrections. Stating `billable` marks it overridden."""

    billable = serializers.BooleanField(required=False)

    class Meta:
        model = Fault
        fields = ["priority", "description", "billable"]


class AssignSerializer(serializers.Serializer[Any]):
    technician = serializers.PrimaryKeyRelatedField(queryset=User.objects.none())

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        technician = cast(serializers.PrimaryKeyRelatedField[User], fields["technician"])
        technician.queryset = _firm_users(self)
        return fields


class ResolveSerializer(serializers.Serializer[Any]):
    resolution_notes = serializers.CharField(required=False, allow_blank=True, default="")
    parts_used = serializers.CharField(required=False, allow_blank=True, default="")
    restore_service = serializers.BooleanField(required=False, default=False)


class CancelSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=1000)


class PublicFaultReportSerializer(serializers.Serializer[Any]):
    """What a resident standing at the lift can tell us, and nothing else."""

    description = serializers.CharField(max_length=1000)
    someone_trapped = serializers.BooleanField(required=False, default=False)
    elevator_stopped = serializers.BooleanField(required=False, default=False)
    reporter_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    reporter_phone = serializers.CharField(
        max_length=30, required=False, allow_blank=True, default=""
    )
    #: The honeypot. Hidden from people by the page; a bot filling every field
    #: fills this one, and is answered exactly like a person while nothing is
    #: stored. Named like a field a form might plausibly have.
    website = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Leave empty. A form that fills this in is treated as automated.",
    )


class PublicFaultReceiptSerializer(serializers.Serializer[Any]):
    reference = serializers.CharField(
        help_text="Six characters to quote when calling about the fault. Nothing else is returned."
    )
