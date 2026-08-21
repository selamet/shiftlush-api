from __future__ import annotations

from typing import Any

import django_filters
from django.db.models import Count, Q, QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.contracts import services
from apps.contracts.api.v1.serializers import (
    AddElevatorsSerializer,
    ContractReadSerializer,
    ContractWriteSerializer,
    RenewSerializer,
    TerminateSerializer,
)
from apps.contracts.models import Contract
from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.permissions import READ, may
from core.viewsets import TenantViewSet


class ContractFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = Contract
        fields = ["status", "customer", "scope"]

    def filter_search(self, queryset: QuerySet[Contract], name: str, value: str):
        return queryset.filter(
            Q(contract_number__icontains=value) | Q(customer__legal_name__icontains=value)
        )


class ContractViewSet(TenantViewSet):
    resource = "contract"
    read_serializer_class = ContractReadSerializer
    write_serializer_class = ContractWriteSerializer
    filterset_class = ContractFilter
    ordering_fields = ["contract_number", "start_date", "end_date", "created_at"]

    def get_base_queryset(self) -> QuerySet[Contract]:
        return Contract.objects.select_related("customer").annotate(
            elevator_count=Count("lines", distinct=True, filter=Q(lines__removed_at__isnull=True))
        )

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        # Decided here, from the same matrix the permission class reads, so the
        # rule lives in one place rather than being restated per serializer.
        context["show_financials"] = may(
            getattr(self.request.user, "role", None), "contract_financials", READ
        )
        return context

    def perform_create(self, serializer) -> None:  # type: ignore[no-untyped-def]
        data = dict(serializer.validated_data)
        if not data.get("contract_number"):
            data["contract_number"] = services.next_contract_number(self.request.user.company_id)
        serializer.save(
            **{"contract_number": data["contract_number"]},
            company_id=self.request.user.company_id,
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_destroy(self, instance: Contract) -> None:
        if instance.lines.filter(removed_at__isnull=True).exists():
            raise RecordInUse(ErrorCode.RECORD_IN_USE)
        super().perform_destroy(instance)

    @extend_schema(request=AddElevatorsSerializer, responses={200: ContractReadSerializer})
    @action(detail=True, methods=["post"], url_path="elevators")
    def add_elevators(self, request: Request, pk: str | None = None) -> Response:
        contract = self.get_object()
        payload = AddElevatorsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.add_elevators(
            contract=contract,
            elevator_ids=[str(value) for value in payload.validated_data["elevator_ids"]],
            unit_price=payload.validated_data.get("unit_price"),
        )
        return Response(self.get_serializer(self.get_object()).data)

    @extend_schema(
        # Declared explicitly: the generator cannot infer a path parameter that
        # is not a field on this model, and would otherwise type it as a plain
        # string in the client.
        parameters=[OpenApiParameter("elevator_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        responses={204: None},
    )
    @action(detail=True, methods=["delete"], url_path=r"elevators/(?P<elevator_id>[^/.]+)")
    def remove_elevator(
        self, request: Request, pk: str | None = None, elevator_id: str | None = None
    ) -> Response:
        services.remove_elevator(contract=self.get_object(), elevator_id=str(elevator_id))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=TerminateSerializer, responses={200: ContractReadSerializer})
    @action(detail=True, methods=["post"])
    def terminate(self, request: Request, pk: str | None = None) -> Response:
        # Its own endpoint rather than a PATCH: it closes every elevator line,
        # moves those elevators to uncontracted and records the reason. A state
        # transition with side effects belongs on the server.
        payload = TerminateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        contract = services.terminate(
            contract=self.get_object(),
            terminated_at=payload.validated_data["terminated_at"],
            reason=payload.validated_data["reason"],
        )
        return Response(self.get_serializer(contract).data)

    @extend_schema(request=RenewSerializer, responses={201: ContractReadSerializer})
    @action(detail=True, methods=["post"])
    def renew(self, request: Request, pk: str | None = None) -> Response:
        payload = RenewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        successor = services.renew(contract=self.get_object(), **payload.validated_data)
        return Response(self.get_serializer(successor).data, status=status.HTTP_201_CREATED)
