from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from apps.maintenance import services
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.users.models import Role, User
from core.error_codes import ErrorCode
from core.requests import authenticated_user
from core.scheduling import local_today


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


# ---------------------------------------------------------------------------
# The monthly plan (M3)
# ---------------------------------------------------------------------------


def _firm_users(serializer: serializers.Serializer[Any]) -> Any:
    """Active users of the signed-in user's firm, or nobody.

    The same scoping SL-124 gave the visit's `technician`: User is the one model
    whose default manager is not tenant-scoped, so a user field is the one
    foreign key that would otherwise accept somebody from another firm. With no
    signed-in user — schema generation — nobody can be named.
    """
    user = getattr(serializer.context.get("request"), "user", None)
    company_id = getattr(user, "company_id", None)
    if company_id is None:
        return User.objects.none()
    return User.objects.filter(company_id=company_id, is_active=True)


class PlannedVisitReadSerializer(serializers.ModelSerializer[PlannedVisit]):
    """One row of the plan, with every name the screen prints.

    The names are joined in by the viewset's select_related, so a page of rows
    is one query — the plan screen and the technician's list both show the
    whole month at once.
    """

    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    building = serializers.UUIDField(source="elevator.building_id", read_only=True)
    building_name = serializers.CharField(source="elevator.building.name", read_only=True)
    # Nullable all the way down: a building may be entered before anyone knows
    # its neighbourhood (see core.serializers.AddressReadMixin).
    neighborhood_name = serializers.CharField(
        source="elevator.building.neighborhood.name", read_only=True, allow_null=True
    )
    district = serializers.IntegerField(
        source="elevator.building.neighborhood.district_id", read_only=True, allow_null=True
    )
    district_name = serializers.CharField(
        source="elevator.building.neighborhood.district.name", read_only=True, allow_null=True
    )
    customer = serializers.UUIDField(source="elevator.building.customer_id", read_only=True)
    customer_name = serializers.CharField(
        source="elevator.building.customer.legal_name", read_only=True
    )
    technician_name = serializers.SerializerMethodField()
    latest_visit_status = serializers.ChoiceField(
        source="visit.status",
        choices=VisitStatus.choices,
        read_only=True,
        allow_null=True,
        help_text=(
            "Status of the visit linked to this row: the one that closed it when "
            "`done`, the latest attempt while `planned`. Null when nobody has "
            "been yet. `planned` with a status here means tried, not done."
        ),
    )
    is_overdue = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField(
        help_text="Days since `due_date` while the row is still planned and past it; null "
        "otherwise. The due date can fall before the row's own month: that is how long "
        "the lift has been overdue."
    )

    class Meta:
        model = PlannedVisit
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "building",
            "building_name",
            "neighborhood_name",
            "district",
            "district_name",
            "customer",
            "customer_name",
            "period",
            "due_date",
            "planned_date",
            "technician",
            "technician_name",
            "status",
            "visit",
            "latest_visit_status",
            "is_overdue",
            "days_overdue",
            "manually_edited",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_technician_name(self, row: PlannedVisit) -> str | None:
        user = row.technician
        if user is None:
            return None
        return f"{user.first_name} {user.last_name}".strip()

    def get_days_overdue(self, row: PlannedVisit) -> int | None:
        if row.status != PlannedVisitStatus.PLANNED:
            return None
        late = (local_today() - row.due_date).days
        return late if late > 0 else None

    def get_is_overdue(self, row: PlannedVisit) -> bool:
        """Still planned and past its due date, by the Istanbul calendar."""
        return row.status == PlannedVisitStatus.PLANNED and row.due_date < local_today()


class PlannedVisitWriteSerializer(StrictMixin, serializers.ModelSerializer[PlannedVisit]):
    # Scoped in get_fields. Null unassigns the row.
    technician = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.none(), allow_null=True, required=False
    )

    class Meta:
        model = PlannedVisit
        fields = ["technician", "planned_date"]

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        technician = cast(serializers.PrimaryKeyRelatedField[User], fields["technician"])
        technician.queryset = _firm_users(self)
        return fields


class BulkAssignSerializer(serializers.Serializer[Any]):
    # Rebound to the tenant-scoped manager in get_fields: another firm's id is
    # "does not exist", the same as one never issued — and one bad id refuses
    # the lot.
    ids = serializers.PrimaryKeyRelatedField(
        queryset=PlannedVisit.objects.none(), many=True, allow_empty=False
    )
    technician = serializers.PrimaryKeyRelatedField(queryset=User.objects.none())

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        # Built per request, not at import: the tenant manager decides which
        # company a queryset belongs to when the queryset is created, and at
        # import time there is no company — the class attribute would match
        # nothing for ever.
        ids = cast(serializers.ManyRelatedField, fields["ids"])
        cast(
            serializers.PrimaryKeyRelatedField[PlannedVisit], ids.child_relation
        ).queryset = PlannedVisit.objects.all()
        technician = cast(serializers.PrimaryKeyRelatedField[User], fields["technician"])
        technician.queryset = _firm_users(self)
        return fields


class BulkAssignResultSerializer(serializers.Serializer[Any]):
    """How many rows the bulk assignment covered, including any already theirs."""

    count = serializers.IntegerField(read_only=True)


class PlanStatusCountsSerializer(serializers.Serializer[Any]):
    planned = serializers.IntegerField()
    done = serializers.IntegerField()
    cancelled = serializers.IntegerField()


@extend_schema_serializer(deprecate_fields=["planned"])
class TechnicianPlanSummarySerializer(serializers.Serializer[Any]):
    """One technician's month. `total` is `done + remaining`; cancelled rows are
    counted apart and in neither."""

    technician = serializers.UUIDField()
    technician_name = serializers.CharField()
    total = serializers.IntegerField(help_text="Rows given to this technician, done or not.")
    done = serializers.IntegerField()
    remaining = serializers.IntegerField(help_text="Rows still planned.")
    overdue = serializers.IntegerField(help_text="Remaining rows past their due date.")
    cancelled = serializers.IntegerField()
    planned = serializers.IntegerField(
        help_text="Deprecated: the same number as `remaining`, under a name that read as "
        "the total. Use `remaining`."
    )


class PlanSummarySerializer(serializers.Serializer[Any]):
    period = serializers.CharField()
    #: Every row of the month, cancelled ones included (unchanged since M3).
    total = serializers.IntegerField()
    done = serializers.IntegerField()
    #: Planned rows, assigned or not.
    remaining = serializers.IntegerField()
    by_status = PlanStatusCountsSerializer()
    #: Planned rows nobody is assigned to.
    unassigned = serializers.IntegerField()
    #: Planned rows past their due date.
    overdue = serializers.IntegerField()
    technicians = TechnicianPlanSummarySerializer(many=True)
