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
from core.filters import TenantFilterSet
from core.serializers import ADDRESS_JOIN
from core.viewsets import TenantViewSet


class BuildingFilter(TenantFilterSet):
    search = django_filters.CharFilter(method="filter_search")
    # Declared rather than derived from the foreign key. django-filter builds a
    # ModelChoiceFilter for an FK and evaluates its queryset when the class is
    # defined — at start-up, with no request and therefore no company in
    # context, where the tenant manager correctly returns nothing. The filter is
    # then born with a permanently empty set of choices and rejects every id.
    #
    # Matching on the raw column also removes a distinction worth not having:
    # an id belonging to another company and an id belonging to nobody now
    # produce the same empty list rather than different answers.
    customer = django_filters.UUIDFilter(field_name="customer_id")
    complex = django_filters.UUIDFilter(field_name="complex_id")
    # Which buildings a route cannot place: the calendar links here with false.
    has_location = django_filters.BooleanFilter(
        method="filter_has_location",
        help_text="true: buildings with both coordinates; false: those missing either.",
    )

    class Meta:
        model = Building
        fields = ["customer", "complex", "type", "is_active", "has_location"]

    def filter_has_location(
        self, queryset: QuerySet[Building], name: str, value: bool
    ) -> QuerySet[Building]:
        located = Q(latitude__isnull=False, longitude__isnull=False)
        return queryset.filter(located) if value else queryset.exclude(located)

    def filter_search(
        self, queryset: QuerySet[Building], name: str, value: str
    ) -> QuerySet[Building]:
        return queryset.filter(
            Q(name__icontains=value)
            | Q(customer__legal_name__icontains=value)
            | Q(address_note__icontains=value)
        )


class ComplexViewSet(TenantViewSet[Complex]):
    resource = "complex"
    read_serializer_class = ComplexReadSerializer
    write_serializer_class = ComplexWriteSerializer
    filterset_fields = ["customer"]
    ordering_fields = ["name", "created_at"]
    # Explicit, not just Meta.ordering: the building/elevator count annotation
    # below puts a GROUP BY on this queryset, and Django does not carry a
    # model's default ordering through one. Without this, two complexes with
    # the same name have no tiebreaker and can swap places between pages.
    ordering = ["name", "id"]
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


class BuildingViewSet(TenantViewSet[Building]):
    resource = "building"
    read_serializer_class = BuildingReadSerializer
    write_serializer_class = BuildingWriteSerializer
    filterset_class = BuildingFilter
    ordering_fields = ["name", "created_at"]
    # See ComplexViewSet.ordering — the elevator count annotation below puts a
    # GROUP BY on this queryset, which the model's default ordering does not
    # survive.
    ordering = ["name", "id"]
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
