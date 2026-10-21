"""Faults, for the firm's own people.

The public report from the QR sticker is a separate view (public_views.py):
it has no user, no tenant from a token, and a threat model of its own.
"""

from __future__ import annotations

from typing import Any

import django_filters
from django.db.models import Case, IntegerField, QuerySet, Value, When
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.faults import services
from apps.faults.api.v1.serializers import (
    AssignSerializer,
    CancelSerializer,
    FaultCreateSerializer,
    FaultEditSerializer,
    FaultReadSerializer,
    ResolveSerializer,
)
from apps.faults.models import Fault, FaultPriority
from apps.users.models import Role
from core.filters import TenantFilterSet
from core.requests import authenticated_user
from core.viewsets import TenantViewSet


class FaultFilter(TenantFilterSet):
    customer = django_filters.UUIDFilter(field_name="elevator__building__customer_id")
    # `reported_at_after` / `reported_at_before`, both inclusive.
    reported_at = django_filters.IsoDateTimeFromToRangeFilter()

    class Meta:
        model = Fault
        fields = ["status", "priority", "channel", "technician", "elevator", "customer"]


class FaultViewSet(TenantViewSet[Fault]):
    """Faults and their transitions.

    Each transition is its own action so that no timestamp can be written by a
    client. PATCH is the office's, and covers only what a person may correct:
    priority, description and the billable flag. There is no DELETE — a fault
    reported by mistake is cancelled, with a reason, and stays on the record.

    `arrive` and `resolve` are open to technicians in the matrix, and narrowed
    here to the one the fault is assigned to; the office may record either on
    their behalf.
    """

    resource = "fault"
    resource_by_action = {
        "update": "fault_office",
        "partial_update": "fault_office",
        "assign": "fault_office",
        "close": "fault_office",
        "cancel": "fault_office",
    }
    http_method_names = ["get", "post", "patch", "head", "options"]
    read_serializer_class = FaultReadSerializer
    write_serializer_class = FaultCreateSerializer
    filterset_class = FaultFilter
    customer_path = "elevator__building__customer_id"
    ordering = ["-reported_at", "-id"]
    # `?ordering=priority_rank,reported_at` is the board: emergencies first,
    # then the oldest — the order somebody should be sent in.
    ordering_fields = ["reported_at", "priority_rank", "resolved_at"]

    def get_base_queryset(self) -> QuerySet[Fault]:
        return Fault.objects.select_related(
            "elevator__building__customer", "technician", "reported_by"
        ).annotate(
            priority_rank=Case(
                When(priority=FaultPriority.EMERGENCY, then=Value(0)),
                When(priority=FaultPriority.HIGH, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )

    def _read(self, fault: Fault, *, code: int = status.HTTP_200_OK) -> Response:
        # Re-read through the viewset's queryset so the response carries every
        # joined name, however the service left the instance.
        fresh = self.get_base_queryset().get(pk=fault.pk)
        return Response(
            self.read_serializer_class(fresh, context=self.get_serializer_context()).data,
            status=code,
        )

    @extend_schema(request=FaultCreateSerializer, responses={201: FaultReadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        write = FaultCreateSerializer(data=request.data, context=self.get_serializer_context())
        write.is_valid(raise_exception=True)
        data = write.validated_data
        fault = services.report_by_staff(
            elevator=data["elevator"],
            description=data["description"],
            priority=data.get("priority", FaultPriority.NORMAL),
            technician=data.get("technician"),
            stopped_elevator=data.get("stopped_elevator", False),
            someone_trapped=data.get("someone_trapped", False),
            reported_by=authenticated_user(request),
        )
        return self._read(fault, code=status.HTTP_201_CREATED)

    @extend_schema(request=FaultEditSerializer, responses={200: FaultReadSerializer})
    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        fault = self.get_object()
        write = FaultEditSerializer(
            fault, data=request.data, partial=True, context=self.get_serializer_context()
        )
        write.is_valid(raise_exception=True)
        services.edit(fault, changes=dict(write.validated_data), by=authenticated_user(request))
        return self._read(fault)

    # PATCH routes to partial_update, and a schema declared on `update` does not
    # reach it: without this the contract described the edit body with the
    # create serializer, which is not what `update` validates.
    @extend_schema(request=FaultEditSerializer, responses={200: FaultReadSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self.update(request, *args, **kwargs)

    @extend_schema(request=AssignSerializer, responses={200: FaultReadSerializer})
    @action(detail=True, methods=["post"])
    def assign(self, request: Request, pk: str | None = None) -> Response:
        """Send a technician, or send a different one. From `open` or `assigned`."""
        fault = self.get_object()
        form = AssignSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        fault = services.assign(
            fault, technician=form.validated_data["technician"], by=authenticated_user(request)
        )
        return self._read(fault)

    def _workable(self, request: Request) -> Fault:
        fault = self.get_object()
        user = authenticated_user(request)
        # Seeing a fault (the customer narrowing) is not the same as being the
        # one sent to it. 403 rather than 404: the fault is visible to them.
        if user.role == Role.TECHNICIAN and fault.technician_id != user.pk:
            raise PermissionDenied
        return fault

    @extend_schema(request=None, responses={200: FaultReadSerializer})
    @action(detail=True, methods=["post"])
    def arrive(self, request: Request, pk: str | None = None) -> Response:
        """The technician is at the lift. From `assigned`."""
        fault = services.arrive(self._workable(request), by=authenticated_user(request))
        return self._read(fault)

    @extend_schema(request=ResolveSerializer, responses={200: FaultReadSerializer})
    @action(detail=True, methods=["post"])
    def resolve(self, request: Request, pk: str | None = None) -> Response:
        """The work is done. From `on_site`. Derives `billable`."""
        fault = self._workable(request)
        form = ResolveSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        fault = services.resolve(fault, by=authenticated_user(request), **form.validated_data)
        return self._read(fault)

    @extend_schema(request=None, responses={200: FaultReadSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request: Request, pk: str | None = None) -> Response:
        """The office has checked the work. From `resolved`."""
        fault = services.close(self.get_object(), by=authenticated_user(request))
        return self._read(fault)

    @extend_schema(request=CancelSerializer, responses={200: FaultReadSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        """A duplicate or a false alarm. Any state before `resolved`; a reason is required."""
        fault = self.get_object()
        form = CancelSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        fault = services.cancel(
            fault, reason=form.validated_data["reason"], by=authenticated_user(request)
        )
        return self._read(fault)
