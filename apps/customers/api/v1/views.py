from __future__ import annotations

import django_filters
from django.db.models import Count, Q, QuerySet

from apps.customers.api.v1.serializers import (
    CustomerContactReadSerializer,
    CustomerContactWriteSerializer,
    CustomerReadSerializer,
    CustomerWriteSerializer,
)
from apps.customers.models import Customer, CustomerContact
from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.viewsets import TenantViewSet


class CustomerFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = Customer
        fields = ["type", "is_active"]

    def filter_search(self, queryset: QuerySet[Customer], name: str, value: str):
        return queryset.filter(
            Q(legal_name__icontains=value)
            | Q(tax_number__startswith=value)
            | Q(phone__contains=value)
        )


class CustomerViewSet(TenantViewSet):
    resource = "customer"
    read_serializer_class = CustomerReadSerializer
    write_serializer_class = CustomerWriteSerializer
    filterset_class = CustomerFilter
    ordering_fields = ["legal_name", "created_at"]
    # The technician sees only assigned customers; on this model the path to
    # Customer is the row itself.
    customer_path = "id"

    def get_base_queryset(self) -> QuerySet[Customer]:
        return (
            Customer.objects.annotate(
                building_count=Count(
                    "buildings", distinct=True, filter=Q(buildings__is_deleted=False)
                ),
                elevator_count=Count(
                    "buildings__elevators",
                    distinct=True,
                    filter=Q(buildings__elevators__is_deleted=False),
                ),
            )
            .select_related("neighborhood")
            .prefetch_related("contacts")
        )

    def perform_destroy(self, instance: Customer) -> None:
        # PROTECT does not fire on a soft delete — that only raises on a real
        # DELETE — so the rule has to be checked here or a customer would be
        # removed out from under its buildings.
        if instance.buildings.exists() or instance.contracts.exists():
            raise RecordInUse(ErrorCode.RECORD_IN_USE)
        super().perform_destroy(instance)


class CustomerContactViewSet(TenantViewSet):
    resource = "customer"
    read_serializer_class = CustomerContactReadSerializer
    write_serializer_class = CustomerContactWriteSerializer
    filterset_fields = ["customer", "role", "is_primary"]
    customer_path = "customer_id"

    def get_base_queryset(self) -> QuerySet[CustomerContact]:
        return CustomerContact.objects.select_related("customer")
