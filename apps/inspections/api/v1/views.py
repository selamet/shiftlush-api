from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

import django_filters
from django.db.models import OuterRef, Prefetch, QuerySet, Subquery
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.elevators.models import Elevator
from apps.inspections import rules, services
from apps.inspections.api.v1.serializers import (
    DueItemSerializer,
    DueType,
    InspectionCreateSerializer,
    InspectionEditSerializer,
    InspectionReadSerializer,
    NonConformityEditSerializer,
    NonConformityEntrySerializer,
    NonConformitySerializer,
)
from apps.inspections.models import Inspection, Label, NonConformity, NonConformityStatus
from apps.users.models import Role, User
from core.filters import TenantFilterSet
from core.permissions import RolePermission
from core.requests import authenticated_user
from core.scheduling import local_today
from core.viewsets import TenantViewSet

#: The window `/inspections/due/` looks ahead by default, and the most it will.
DUE_WINDOW_DEFAULT = 60
DUE_WINDOW_MAX = 365


class InspectionFilter(TenantFilterSet):
    # A plain UUID filter, not the ModelChoiceFilter django-filter would derive:
    # that one builds its queryset from the tenant manager when the class is
    # defined, outside any request, and so matches nothing and answers 400.
    elevator = django_filters.UUIDFilter(field_name="elevator_id")
    # Inclusive. The next periodic inspection on or before this day.
    due_before = django_filters.DateFilter(field_name="next_inspection_due", lookup_expr="lte")
    # Inclusive. A correction deadline on or before this day.
    correction_before = django_filters.DateFilter(
        field_name="correction_deadline", lookup_expr="lte"
    )
    has_open_nonconformities = django_filters.BooleanFilter(method="filter_open")

    class Meta:
        model = Inspection
        fields = [
            "elevator",
            "label",
            "kind",
            "due_before",
            "correction_before",
            "has_open_nonconformities",
        ]

    def filter_open(
        self, queryset: QuerySet[Inspection], name: str, value: bool
    ) -> QuerySet[Inspection]:
        condition = services.has_open_nonconformities()
        return queryset.filter(condition) if value else queryset.exclude(condition)


def _assigned_customers(user: Any) -> list[Any] | None:
    """The customers a technician is narrowed to, or None for everyone else."""
    if isinstance(user, User) and user.role == Role.TECHNICIAN:
        return list(user.customer_assignments.values_list("customer_id", flat=True))
    return None


class InspectionViewSet(TenantViewSet[Inspection]):
    """Inspections, recorded by the office and read by technicians.

    No DELETE: an inspection is a third party's statement about the lift, and a
    wrong one is corrected (PATCH), not removed.
    """

    resource = "inspection"
    http_method_names = ["get", "post", "patch", "head", "options"]
    read_serializer_class = InspectionReadSerializer
    write_serializer_class = InspectionCreateSerializer
    filterset_class = InspectionFilter
    customer_path = "elevator__building__customer_id"
    ordering = ["-inspected_on", "-id"]
    ordering_fields = ["inspected_on", "next_inspection_due", "correction_deadline"]

    def get_base_queryset(self) -> QuerySet[Inspection]:
        return Inspection.objects.select_related(
            "elevator__building__customer", "follows"
        ).prefetch_related(
            Prefetch("nonconformities", queryset=NonConformity.objects.order_by("created_at", "id"))
        )

    def _read(self, inspection: Inspection, code: int = status.HTTP_200_OK) -> Response:
        fresh = self.get_base_queryset().get(pk=inspection.pk)
        return Response(
            self.read_serializer_class(fresh, context=self.get_serializer_context()).data,
            status=code,
        )

    @extend_schema(request=InspectionCreateSerializer, responses={201: InspectionReadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = InspectionCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        data = dict(form.validated_data)
        inspection = services.record(by=authenticated_user(request), **data)
        return self._read(inspection, status.HTTP_201_CREATED)

    @extend_schema(request=InspectionEditSerializer, responses={200: InspectionReadSerializer})
    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        inspection = self.get_object()
        form = InspectionEditSerializer(
            inspection, data=request.data, partial=True, context=self.get_serializer_context()
        )
        form.is_valid(raise_exception=True)
        services.edit(inspection, changes=dict(form.validated_data), by=authenticated_user(request))
        return self._read(inspection)

    @extend_schema(request=NonConformityEntrySerializer, responses={201: NonConformitySerializer})
    @action(detail=True, methods=["post"], url_path="nonconformities", url_name="nonconformities")
    def add_nonconformity(self, request: Request, pk: str | None = None) -> Response:
        """Record one more defect the inspector found."""
        inspection = self.get_object()
        form = NonConformityEntrySerializer(data=request.data)
        form.is_valid(raise_exception=True)
        nonconformity = services.add_nonconformity(
            inspection, by=authenticated_user(request), **form.validated_data
        )
        return Response(NonConformitySerializer(nonconformity).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "days",
                OpenApiTypes.INT,
                OpenApiParameter.QUERY,
                description=(
                    f"How far ahead to look, in days (default {DUE_WINDOW_DEFAULT}, "
                    f"at most {DUE_WINDOW_MAX}). Anything already overdue is always included."
                ),
            )
        ],
        responses={200: DueItemSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], pagination_class=None, filter_backends=[])
    def due(self, request: Request) -> Response:
        """What is due across the firm: inspections, and correction deadlines.

        Inspections come from each lift's next inspection date (which the latest
        inspection sets, or Phase 1 data where none is recorded). Corrections are
        the yellow and red deadlines of each lift's current inspection while a
        defect is still open. Overdue items stay on the list until dealt with —
        they are the ones that end in a seal. Ordered by date.
        """
        try:
            window = int(request.query_params.get("days", DUE_WINDOW_DEFAULT))
        except ValueError:
            raise serializers.ValidationError(
                {"days": serializers.ErrorDetail("A whole number of days.", "invalid")}
            ) from None
        window = max(0, min(window, DUE_WINDOW_MAX))
        today = local_today()
        horizon = today + timedelta(days=window)
        assigned = _assigned_customers(request.user)

        latest = Inspection.objects.filter(elevator_id=OuterRef("pk")).order_by(
            "-inspected_on", "-created_at", "-id"
        )
        lifts = (
            Elevator.objects.filter(
                next_inspection_date__isnull=False, next_inspection_date__lte=horizon
            )
            .select_related("building__customer")
            .annotate(latest_inspection=Subquery(latest.values("id")[:1]))
            .order_by("next_inspection_date", "id")
        )
        corrections = (
            services.current_inspections()
            .filter(
                label__in=[Label.YELLOW, Label.RED],
                correction_deadline__isnull=False,
                correction_deadline__lte=horizon,
            )
            .select_related("elevator__building__customer")
            .order_by("correction_deadline", "id")
        )
        if assigned is not None:
            lifts = lifts.filter(building__customer_id__in=assigned)
            corrections = corrections.filter(elevator__building__customer_id__in=assigned)

        rows: list[dict[str, Any]] = []
        for lift in lifts:
            assert lift.next_inspection_date is not None
            rows.append(
                {
                    "type": DueType.INSPECTION,
                    "date": lift.next_inspection_date,
                    "days_left": (lift.next_inspection_date - today).days,
                    "elevator": lift.id,
                    "elevator_name": lift.name,
                    "building_name": lift.building.name,
                    "customer_name": lift.building.customer.legal_name,
                    "label": lift.inspection_label,
                    "inspection": lift.latest_inspection,
                    "open_nonconformities": 0,
                    # A missed periodic inspection is not itself a sealing rule
                    # in the table; the deadlines are.
                    "sealed_if_missed": False,
                }
            )

        current = list(corrections)
        # One query for every open-defect count, the follow-ups' origins included.
        relevant = {i.pk for i in current} | {i.follows_id for i in current if i.follows_id}
        open_counts = Counter(
            NonConformity.objects.filter(
                inspection_id__in=relevant, status=NonConformityStatus.OPEN
            ).values_list("inspection_id", flat=True)
        )
        for inspection in current:
            assert inspection.correction_deadline is not None
            still_open = open_counts[inspection.pk] + (
                open_counts[inspection.follows_id] if inspection.follows_id else 0
            )
            if not still_open:
                continue
            elevator = inspection.elevator
            rows.append(
                {
                    "type": DueType.CORRECTION,
                    "date": inspection.correction_deadline,
                    "days_left": (inspection.correction_deadline - today).days,
                    "elevator": elevator.id,
                    "elevator_name": elevator.name,
                    "building_name": elevator.building.name,
                    "customer_name": elevator.building.customer.legal_name,
                    "label": inspection.label,
                    "inspection": inspection.pk,
                    "open_nonconformities": still_open,
                    "sealed_if_missed": rules.RULES[Label(inspection.label)].sealed_if_missed,
                }
            )
        rows.sort(key=lambda row: (row["date"], row["type"], str(row["elevator"])))
        return Response(DueItemSerializer(rows, many=True).data)


class NonConformityViewSet(
    mixins.RetrieveModelMixin, mixins.UpdateModelMixin, GenericViewSet[NonConformity]
):
    """A defect an inspection found: fixed, waived, or sent to a technician."""

    resource = "inspection"
    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = NonConformity.objects.none()
    serializer_class = NonConformitySerializer

    def get_queryset(self) -> QuerySet[NonConformity]:
        queryset = NonConformity.objects.select_related("inspection")
        assigned = _assigned_customers(self.request.user)
        if assigned is not None:
            queryset = queryset.filter(inspection__elevator__building__customer_id__in=assigned)
        return queryset

    @extend_schema(request=NonConformityEditSerializer, responses={200: NonConformitySerializer})
    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        nonconformity = self.get_object()
        form = NonConformityEditSerializer(nonconformity, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        services.edit_nonconformity(
            nonconformity, changes=dict(form.validated_data), by=authenticated_user(request)
        )
        return Response(NonConformitySerializer(nonconformity).data)

    @extend_schema(
        request=None,
        responses={201: NonConformitySerializer, 200: NonConformitySerializer},
    )
    @action(detail=True, methods=["post"], url_path="open-fault")
    def open_fault(self, request: Request, pk: str | None = None) -> Response:
        """Open an M4 fault on the lift to fix this defect, and link it.

        201 with the fault's id in `fault` when one was opened; 200 when the
        defect already had one, which is returned instead of a second.
        """
        nonconformity = self.get_object()
        had_one = nonconformity.fault_id is not None
        services.open_fault(nonconformity, by=authenticated_user(request))
        nonconformity.refresh_from_db()
        return Response(
            NonConformitySerializer(nonconformity).data,
            status=status.HTTP_200_OK if had_one else status.HTTP_201_CREATED,
        )
