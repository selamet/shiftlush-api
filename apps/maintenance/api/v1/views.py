from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, TypeVar, cast

import django_filters
from django.core.validators import RegexValidator
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import GenericViewSet

from apps.maintenance import services
from apps.maintenance.api.v1.serializers import (
    BulkAssignResultSerializer,
    BulkAssignSerializer,
    MaintenanceVisitReadSerializer,
    MaintenanceVisitWriteSerializer,
    PlannedVisitReadSerializer,
    PlannedVisitWriteSerializer,
    PlanSummarySerializer,
    SetChecklistSerializer,
    TechnicianOptionSerializer,
)
from apps.maintenance.models import MaintenanceVisit, PlannedVisit, PlannedVisitStatus
from apps.users.models import Role, User
from core.filters import TenantFilterSet
from core.permissions import RolePermission
from core.requests import authenticated_user
from core.scheduling import local_today
from core.viewsets import TenantViewSet


class MaintenanceVisitFilter(TenantFilterSet):
    customer = django_filters.UUIDFilter(field_name="elevator__building__customer_id")
    # `visit_date_after` / `visit_date_before`, both inclusive.
    visit_date = django_filters.DateFromToRangeFilter()

    class Meta:
        model = MaintenanceVisit
        fields = ["elevator", "customer", "status", "technician"]


class MaintenanceVisitViewSet(TenantViewSet[MaintenanceVisit]):
    resource = "maintenance_visit"
    resource_by_action = {
        "technicians": "maintenance_visit",
        "update": "maintenance_visit_edit",
        "partial_update": "maintenance_visit_edit",
        "destroy": "maintenance_visit_edit",
    }
    # Visits are sent from a phone's outbox and retried on a bad connection;
    # without a key, a retry records the visit twice (spec 8.10).
    idempotency_key_required = True
    # A phone can hold a visit in its outbox for days. If the first send landed
    # and only the response was lost, the retry must still find its key (#99).
    idempotency_retention = timedelta(days=30)
    read_serializer_class = MaintenanceVisitReadSerializer
    write_serializer_class = MaintenanceVisitWriteSerializer
    filterset_class = MaintenanceVisitFilter
    ordering_fields = ["visit_date", "created_at"]
    ordering = ["-visit_date", "id"]
    customer_path = "elevator__building__customer_id"

    def get_base_queryset(self) -> QuerySet[MaintenanceVisit]:
        return MaintenanceVisit.objects.select_related(
            "elevator", "contract", "technician"
        ).prefetch_related("items")

    def perform_create(self, serializer: BaseSerializer[MaintenanceVisit]) -> None:
        elevator = serializer.validated_data["elevator"]
        contract = serializer.validated_data["contract"]
        services.ensure_active_contract(elevator=elevator, contract=contract)
        super().perform_create(serializer)
        services.recalculate_next_maintenance(elevator)
        assert isinstance(serializer.instance, MaintenanceVisit)
        services.sync_plan_for_visit(serializer.instance)

    def perform_update(self, serializer: BaseSerializer[MaintenanceVisit]) -> None:
        instance = serializer.instance
        assert instance is not None
        elevator = serializer.validated_data.get("elevator", instance.elevator)
        contract = serializer.validated_data.get("contract", instance.contract)
        services.ensure_active_contract(elevator=elevator, contract=contract)
        # Where the visit was before the edit, so the plan row it closed there
        # is reopened if the edit moved it to another month or lift.
        before = (instance.elevator_id, instance.visit_date)
        previous_elevator = instance.elevator
        super().perform_update(serializer)
        services.recalculate_next_maintenance(elevator)
        # A visit moved to another lift no longer counts for the one it left;
        # without this, that lift kept a next date earned by a visit it lost.
        if previous_elevator.pk != elevator.pk:
            services.recalculate_next_maintenance(previous_elevator)
        services.sync_plan_for_visit(instance, before=before)

    def perform_destroy(self, instance: MaintenanceVisit) -> None:
        elevator = instance.elevator
        super().perform_destroy(instance)
        services.recalculate_next_maintenance(elevator)
        # The visit is gone from the month now, so this reopens the row it closed.
        services.sync_plan_for_visit(instance)

    # Atomic for the same reason as create and update: the visit, the lift's
    # maintenance dates and the plan row it closed change together or not at all.
    @transaction.atomic
    def destroy(self, request: Request, *args: object, **kwargs: object) -> Response:
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=True,
                description=(
                    "Generated once per visit form and reused on every retry, so a "
                    "visit sent again from a device outbox is recorded once."
                ),
            )
        ],
        # The view answers with the read shape, id included; without this the
        # contract described the request body as the response.
        responses={201: MaintenanceVisitReadSerializer},
    )
    @transaction.atomic
    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        self.perform_create(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)

    # PATCH routes to partial_update; the schema has to be declared there too.
    @extend_schema(responses={200: MaintenanceVisitReadSerializer})
    def partial_update(self, request: Request, *args: object, **kwargs: object) -> Response:
        return self.update(request, *args, **kwargs)

    @extend_schema(responses={200: MaintenanceVisitReadSerializer})
    @transaction.atomic
    def update(self, request: Request, *args: object, **kwargs: object) -> Response:
        instance = self.get_object()
        write = self.get_serializer(instance, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        self.perform_update(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data)

    @extend_schema(responses={200: TechnicianOptionSerializer(many=True)})
    @action(detail=False, methods=["get"], pagination_class=None)
    def technicians(self, request: Request) -> Response:
        """The firm's active technicians, by name, for the visit form.

        The user list is owners' and admins' only — it carries e-mail addresses,
        phone numbers and roles. Operations records visits too, and needs to
        say who did the work; this answers exactly that and nothing more.
        """
        company_id = authenticated_user(request).company_id
        if company_id is None:
            return Response([])
        technicians = User.objects.filter(
            company_id=company_id, role=Role.TECHNICIAN, is_active=True
        ).order_by("first_name", "last_name", "id")
        rows = [
            {"id": user.id, "name": f"{user.first_name} {user.last_name}".strip()}
            for user in technicians
        ]
        return Response(TechnicianOptionSerializer(rows, many=True).data)

    @action(detail=True, methods=["post"], url_path="set-checklist")
    def set_checklist(self, request: Request, pk: str | None = None) -> Response:
        """Replace this visit's checklist rows with exactly what is sent.

        A second call, matching how Contract's elevators are managed via
        add-elevators rather than nested inside the contract payload - see
        the plan's file-structure note on why this diverges from the design
        doc's original nested-serializer sketch.
        """
        visit = self.get_object()
        form = SetChecklistSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        services.set_checklist(visit=visit, items=form.validated_data["items"])
        visit.updated_by = authenticated_user(request)
        visit.save(update_fields=["updated_by", "updated_at"])
        read = self.read_serializer_class(visit, context=self.get_serializer_context())
        return Response(read.data)


# ---------------------------------------------------------------------------
# The monthly plan (M3)
# ---------------------------------------------------------------------------


class PlannedVisitFilter(TenantFilterSet):
    period = django_filters.CharFilter(
        required=True,
        validators=[RegexValidator(r"^\d{4}-(0[1-9]|1[0-2])$", code="invalid")],
        help_text='The month, as "YYYY-MM". Required: the plan is always read a month at a time.',
    )
    technician = django_filters.CharFilter(
        method="filter_technician",
        help_text='A user id, or "me" for the signed-in user.',
    )
    unassigned = django_filters.BooleanFilter(method="filter_unassigned")
    # Inclusive, like visit_date_before on the visit list.
    due_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")
    district = django_filters.NumberFilter(field_name="elevator__building__neighborhood__district")
    customer = django_filters.UUIDFilter(field_name="elevator__building__customer_id")
    # The calendar's week: the rows planned for a range of days (inclusive), and
    # the tray of rows with no day yet. A range never matches a row with no day.
    planned_from = django_filters.DateFilter(
        field_name="planned_date",
        lookup_expr="gte",
        help_text="Rows planned on or after this day. Rows with no planned day are left out.",
    )
    planned_to = django_filters.DateFilter(
        field_name="planned_date",
        lookup_expr="lte",
        help_text="Rows planned on or before this day. Rows with no planned day are left out.",
    )
    unscheduled = django_filters.BooleanFilter(
        field_name="planned_date",
        lookup_expr="isnull",
        help_text="true: only rows with no planned day; false: only rows with one.",
    )

    class Meta:
        model = PlannedVisit
        fields = [
            "period",
            "status",
            "elevator",
            "technician",
            "unassigned",
            "due_before",
            "district",
            "customer",
            "planned_from",
            "planned_to",
            "unscheduled",
        ]

    def filter_technician(
        self, queryset: QuerySet[PlannedVisit], name: str, value: str
    ) -> QuerySet[PlannedVisit]:
        # "me" so the technician's own list needs no second request to learn
        # their id, and stays the same URL on every phone.
        if value == "me":
            return queryset.filter(technician_id=getattr(self.request.user, "pk", None))
        try:
            technician_id = uuid.UUID(value)
        except ValueError:
            raise serializers.ValidationError(
                {"technician": serializers.ErrorDetail("Must be a user id or 'me'.", "invalid")}
            ) from None
        return queryset.filter(technician_id=technician_id)

    def filter_unassigned(
        self, queryset: QuerySet[PlannedVisit], name: str, value: bool
    ) -> QuerySet[PlannedVisit]:
        return queryset.filter(technician__isnull=value)


# The row type a queryset yields, kept generic so filter_queryset matches the
# signature DRF's stubs declare for it.
RowT = TypeVar("RowT")

#: Route order: a technician works a district, then a neighbourhood, then a
#: building, and the plan reads the way they drive. Not a solver (non-goal).
ROUTE_ORDER = [
    "elevator__building__neighborhood__district__name",
    "elevator__building__neighborhood__name",
    "elevator__building__name",
    "elevator__name",
    "id",
]


class PlannedVisitViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet[PlannedVisit],
):
    """The monthly maintenance plan.

    Composed from mixins rather than a TenantViewSet because nothing here is
    created or deleted by a client — the `plan_maintenance` job writes the rows
    and a recorded visit closes them — so there is no create for the router to
    publish, and no delete.

    Technicians read their own rows and the unassigned rows of the customers
    they are assigned to: the usual customer narrowing, then within it only
    what is theirs to do or still open for anyone. Another technician's row
    is a 404.
    """

    resource = "planned_visit"
    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    # Never served: get_queryset() replaces it. `none()` so that losing the
    # override empties the plan rather than opening it.
    queryset = PlannedVisit.objects.none()
    serializer_class = PlannedVisitReadSerializer
    write_serializer_class = PlannedVisitWriteSerializer
    filterset_class = PlannedVisitFilter
    ordering = ROUTE_ORDER
    ordering_fields = ["due_date", "planned_date"]

    def get_queryset(self) -> QuerySet[PlannedVisit]:
        queryset = PlannedVisit.objects.select_related(
            "elevator__building__customer",
            "elevator__building__neighborhood__district",
            "technician",
            "visit",
        )
        user = self.request.user
        # isinstance, not authenticated_user(): the schema generator walks this
        # without a request user, and the answer there is "no narrowing".
        if isinstance(user, User) and user.role == Role.TECHNICIAN:
            assigned = list(user.customer_assignments.values_list("customer_id", flat=True))
            queryset = queryset.filter(elevator__building__customer_id__in=assigned).filter(
                Q(technician=user) | Q(technician__isnull=True)
            )
        return queryset

    def filter_queryset(
        self, queryset: QuerySet[PlannedVisit, RowT]
    ) -> QuerySet[PlannedVisit, RowT]:
        # The filters (period required among them) describe a month's list. A
        # row addressed by id is found by id alone; without this, opening or
        # editing one would demand a ?period= that names nothing.
        if self.action not in ("list", "summary"):
            return queryset
        return super().filter_queryset(queryset)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action in ("update", "partial_update"):
            return self.write_serializer_class
        return self.serializer_class

    # PATCH routes to partial_update; the schema has to be declared there too.
    @extend_schema(request=PlannedVisitWriteSerializer, responses={200: PlannedVisitReadSerializer})
    def partial_update(self, request: Request, *args: object, **kwargs: object) -> Response:
        return self.update(request, *args, **kwargs)

    @extend_schema(request=PlannedVisitWriteSerializer, responses={200: PlannedVisitReadSerializer})
    @transaction.atomic
    def update(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Change the technician or the planned date. Marks the row as edited."""
        row = self.get_object()
        write = PlannedVisitWriteSerializer(
            row, data=request.data, partial=True, context=self.get_serializer_context()
        )
        write.is_valid(raise_exception=True)
        services.edit_planned_visit(
            row, changes=dict(write.validated_data), editor=authenticated_user(request)
        )
        return Response(PlannedVisitReadSerializer(row, context=self.get_serializer_context()).data)

    @extend_schema(request=BulkAssignSerializer, responses={200: BulkAssignResultSerializer})
    @action(detail=False, methods=["post"], url_path="bulk-assign", pagination_class=None)
    def bulk_assign(self, request: Request) -> Response:
        """Give many rows to one technician, all or nothing.

        One id the caller cannot see — another firm's, or never issued — refuses
        the whole request with nothing changed. Converges on a retry: rows
        already given to that technician are not announced again, so it needs
        no Idempotency-Key.
        """
        form = BulkAssignSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        rows = list({row.pk: row for row in form.validated_data["ids"]}.values())
        count = services.bulk_assign(
            rows,
            technician=form.validated_data["technician"],
            editor=authenticated_user(request),
        )
        return Response(BulkAssignResultSerializer({"count": count}).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "period",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                description='The month, as "YYYY-MM". The list filters apply here too.',
            )
        ],
        responses={200: PlanSummarySerializer},
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def summary(self, request: Request) -> Response:
        """Counts for the plan screen's header: per technician, unassigned, overdue.

        Over the same rows the list would return for the same filters, so a
        technician's summary covers exactly the rows they can see.
        """
        rows = self.filter_queryset(self.get_queryset()).order_by()
        today = local_today()
        planned = Q(status=PlannedVisitStatus.PLANNED)
        overdue = planned & Q(due_date__lt=today)
        per_status = {
            "planned": Count("id", filter=planned),
            "done": Count("id", filter=Q(status=PlannedVisitStatus.DONE)),
            "cancelled": Count("id", filter=Q(status=PlannedVisitStatus.CANCELLED)),
        }
        totals = rows.aggregate(
            total=Count("id"),
            unassigned=Count("id", filter=planned & Q(technician__isnull=True)),
            overdue=Count("id", filter=overdue),
            **per_status,
        )
        grouped = (
            rows.filter(technician__isnull=False)
            .values("technician", "technician__first_name", "technician__last_name")
            .annotate(overdue=Count("id", filter=overdue), **per_status)
            .order_by("technician__first_name", "technician__last_name", "technician")
        )
        # Annotated with names built at runtime, which the stubs cannot follow.
        technicians = cast(Iterable[dict[str, Any]], grouped)
        body = {
            "period": request.query_params["period"],
            "total": totals["total"],
            "by_status": {name: totals[name] for name in per_status},
            "done": totals["done"],
            "remaining": totals["planned"],
            "unassigned": totals["unassigned"],
            "overdue": totals["overdue"],
            "technicians": [
                {
                    "technician": entry["technician"],
                    "technician_name": (
                        f"{entry['technician__first_name']} {entry['technician__last_name']}"
                    ).strip(),
                    "total": entry["planned"] + entry["done"],
                    "done": entry["done"],
                    "remaining": entry["planned"],
                    "overdue": entry["overdue"],
                    "cancelled": entry["cancelled"],
                    "planned": entry["planned"],
                }
                for entry in technicians
            ],
        }
        return Response(PlanSummarySerializer(body).data)
