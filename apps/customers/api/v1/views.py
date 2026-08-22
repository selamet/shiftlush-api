from __future__ import annotations

import django_filters
from django.db.models import Count, Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.customers.api.v1.serializers import (
    CustomerContactNestedWriteSerializer,
    CustomerContactReadSerializer,
    CustomerContactWriteSerializer,
    CustomerReadSerializer,
    CustomerWriteSerializer,
)
from apps.customers.models import Customer, CustomerContact
from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.idempotency import replay_protected
from core.serializers import ADDRESS_JOIN
from core.text import normalize
from core.viewsets import TenantViewSet


class CustomerFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = Customer
        fields = ["type", "is_active"]

    def filter_search(self, queryset: QuerySet[Customer], name: str, value: str):
        # Through the normalised copy of the name, never the name itself.
        # `icontains` on `legal_name` is the trap in section 9.2: the user types
        # a name in plain ASCII, the record holds it with cedillas and breves,
        # and nothing matches and nothing complains. Both sides go through the
        # same fold.
        return queryset.filter(
            Q(legal_name_normalized__contains=normalize(value))
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
            .select_related(*ADDRESS_JOIN)
            .prefetch_related("contacts")
        )

    def perform_destroy(self, instance: Customer) -> None:
        # PROTECT does not fire on a soft delete — that only raises on a real
        # DELETE — so the rule has to be checked here or a customer would be
        # removed out from under its buildings.
        if instance.buildings.exists() or instance.contracts.exists():
            raise RecordInUse(ErrorCode.RECORD_IN_USE)
        super().perform_destroy(instance)

    # Annotated per method, and the list response is declared `many=True` so the
    # generator pages it. Left to itself it described both verbs as returning a
    # single customer, which reaches the frontend as a compiling, wrong type.
    @extend_schema(
        methods=["GET"],
        description="The contacts of this customer.",
        # `filters=False` because the customer filters do not apply here and are
        # not honoured. Left on, the contract would advertise a `search` this
        # endpoint silently ignores.
        filters=False,
        responses={200: CustomerContactReadSerializer(many=True)},
    )
    @extend_schema(
        methods=["POST"],
        description="Add a contact to this customer. The customer comes from the path.",
        request=CustomerContactNestedWriteSerializer,
        responses={201: CustomerContactReadSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="contacts")
    def contacts(self, request: Request, pk: str | None = None) -> Response:
        """The contacts of one customer — specification §8.6.

        The flat `/customer-contacts` endpoint stays; this is the path a client
        already holding a customer reaches for, and it cannot address the wrong
        customer because the id comes from the URL rather than the body.

        `get_object()` is what makes another company's customer a 404 here, the
        same as everywhere else — the tenant filter and the technician narrowing
        both live in the queryset it reads.
        """
        customer = self.get_object()
        if request.method == "POST":
            return self._create_contact(request, customer)

        queryset = CustomerContact.objects.filter(customer=customer)
        page = self.paginate_queryset(queryset)
        serializer = CustomerContactReadSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @replay_protected
    def _create_contact(self, request: Request, customer: Customer) -> Response:
        write = CustomerContactNestedWriteSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        write.is_valid(raise_exception=True)
        write.save(
            company_id=request.user.company_id,
            customer=customer,
            created_by=request.user,
            updated_by=request.user,
        )
        read = CustomerContactReadSerializer(write.instance, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)


class CustomerContactViewSet(TenantViewSet):
    resource = "customer"
    read_serializer_class = CustomerContactReadSerializer
    write_serializer_class = CustomerContactWriteSerializer
    filterset_fields = ["customer", "role", "is_primary"]
    customer_path = "customer_id"

    def get_base_queryset(self) -> QuerySet[CustomerContact]:
        return CustomerContact.objects.select_related("customer")
