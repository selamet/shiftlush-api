from __future__ import annotations

import django_filters
from django.db.models import Count, Q, QuerySet

from apps.properties.api.v1.serializers import (
    BuildingReadSerializer,
    BuildingWriteSerializer,
    ComplexReadSerializer,
    ComplexWriteSerializer,
)
from apps.properties.models import Building, Complex
from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.viewsets import TenantViewSet

ADDRESS_JOIN = ("neighborhood", "neighborhood__district", "neighborhood__district__province")


class BuildingFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = Building
        fields = ["customer", "complex", "type", "is_active"]

    def filter_search(self, queryset: QuerySet[Building], name: str, value: str):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(customer__legal_name__icontains=value)
            | Q(address_note__icontains=value)
        )


class ComplexViewSet(TenantViewSet):
    resource = "complex"
    read_serializer_class = ComplexReadSerializer
    write_serializer_class = ComplexWriteSerializer
    filterset_fields = ["customer"]
    ordering_fields = ["name", "created_at"]
    customer_path = "customer_id"

    def get_base_queryset(self) -> QuerySet[Complex]:
        return Complex.objects.annotate(
            building_count=Count("buildings", distinct=True, filter=Q(buildings__is_deleted=False)),
            elevator_count=Count(
                "buildings__elevators",
                distinct=True,
                filter=Q(buildings__elevators__is_deleted=False),
            ),
        ).select_related("customer", *ADDRESS_JOIN)

    def perform_destroy(self, instance: Complex) -> None:
        if instance.buildings.exists():
            raise RecordInUse(ErrorCode.RECORD_IN_USE)
        super().perform_destroy(instance)


class BuildingViewSet(TenantViewSet):
    resource = "building"
    read_serializer_class = BuildingReadSerializer
    write_serializer_class = BuildingWriteSerializer
    filterset_class = BuildingFilter
    ordering_fields = ["name", "created_at"]
    customer_path = "customer_id"

    def get_base_queryset(self) -> QuerySet[Building]:
        # select_related on every join the serializer reads. Without it a page
        # of 25 buildings issues 100+ queries, and the list screen is the one
        # place this product cannot be slow.
        return Building.objects.annotate(
            elevator_count=Count("elevators", distinct=True, filter=Q(elevators__is_deleted=False))
        ).select_related("customer", "complex", *ADDRESS_JOIN)

    def perform_destroy(self, instance: Building) -> None:
        # Named in the acceptance criteria: a building that still has elevators
        # cannot be deleted, and the answer has to say why.
        if instance.elevators.exists():
            raise RecordInUse(ErrorCode.RECORD_IN_USE)
        super().perform_destroy(instance)
