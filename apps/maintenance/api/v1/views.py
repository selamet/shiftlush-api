from __future__ import annotations

import django_filters
from django.db import transaction
from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from apps.maintenance import services
from apps.maintenance.api.v1.serializers import (
    MaintenanceVisitReadSerializer,
    MaintenanceVisitWriteSerializer,
    SetChecklistSerializer,
    TechnicianOptionSerializer,
)
from apps.maintenance.models import MaintenanceVisit
from apps.users.models import Role, User
from core.requests import authenticated_user
from core.viewsets import TenantViewSet


class MaintenanceVisitFilter(django_filters.FilterSet):
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

    def perform_update(self, serializer: BaseSerializer[MaintenanceVisit]) -> None:
        instance = serializer.instance
        assert instance is not None
        elevator = serializer.validated_data.get("elevator", instance.elevator)
        contract = serializer.validated_data.get("contract", instance.contract)
        services.ensure_active_contract(elevator=elevator, contract=contract)
        super().perform_update(serializer)
        services.recalculate_next_maintenance(elevator)

    def perform_destroy(self, instance: MaintenanceVisit) -> None:
        elevator = instance.elevator
        super().perform_destroy(instance)
        services.recalculate_next_maintenance(elevator)

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
