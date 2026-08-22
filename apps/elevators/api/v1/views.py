from __future__ import annotations

import django_filters
from django.db.models import Q, QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.elevators.api.v1.serializers import (
    ElevatorByQrSerializer,
    ElevatorDetailSerializer,
    ElevatorListSerializer,
    ElevatorWriteSerializer,
    LabelRequestSerializer,
)
from apps.elevators.labels import PdfRenderingUnavailable, render_labels
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token, regenerate_qr_token
from core.error_codes import ErrorCode
from core.exceptions import RecordInUse, ServiceUnavailable
from core.idempotency import replay_protected
from core.permissions import RolePermission
from core.viewsets import TenantViewSet

JOINS = ("building", "building__customer", "building__complex")


class ElevatorFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search")
    customer = django_filters.UUIDFilter(field_name="building__customer_id")
    # Same reason as `customer` above, which was already declared by hand.
    building = django_filters.UUIDFilter(field_name="building_id")

    class Meta:
        model = Elevator
        fields = ["status", "inspection_label", "category", "building", "customer"]

    def filter_search(self, queryset: QuerySet[Elevator], name: str, value: str):
        return queryset.filter(
            Q(registration_number__icontains=value)
            | Q(name__icontains=value)
            | Q(internal_code__icontains=value)
            | Q(building__name__icontains=value)
        )


class ElevatorViewSet(TenantViewSet):
    resource = "elevator"
    # Printing a label and regenerating a token are field work: a technician who
    # has to go back to the office for a replacement sticker is a technician who
    # leaves the lift unlabelled.
    resource_by_action = {"labels": "qr_label", "regenerate_qr": "qr_label"}
    read_serializer_class = ElevatorDetailSerializer
    write_serializer_class = ElevatorWriteSerializer
    filterset_class = ElevatorFilter
    ordering_fields = ["registration_number", "name", "next_inspection_date", "created_at"]
    customer_path = "building__customer_id"

    def get_serializer_class(self):  # type: ignore[no-untyped-def]
        # The list screen carries 500+ rows; sending all 31 fields per row would
        # make the heaviest screen heavier for data it does not draw.
        if self.action == "list":
            return ElevatorListSerializer
        return super().get_serializer_class()

    # Creating one of these twice from a retry is the complaint field software
    # gets most, and both copies look legitimate afterwards.
    @replay_protected
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().create(request, *args, **kwargs)

    def get_base_queryset(self) -> QuerySet[Elevator]:
        return Elevator.objects.select_related(*JOINS)

    def perform_create(self, serializer) -> None:  # type: ignore[no-untyped-def]
        # Every elevator gets a token at birth: the label can be printed the
        # moment the record exists, and a nullable token would mean every
        # consumer has to handle the empty case forever.
        elevator = Elevator(**serializer.validated_data)
        assign_qr_token(elevator)
        serializer.save(
            qr_token=elevator.qr_token,
            company_id=self.request.user.company_id,
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_destroy(self, instance: Elevator) -> None:
        if instance.contract_lines.filter(removed_at__isnull=True).exists():
            raise RecordInUse(ErrorCode.ELEVATOR_ALREADY_CONTRACTED)
        super().perform_destroy(instance)

    @extend_schema(request=None, responses={200: ElevatorDetailSerializer})
    @action(detail=True, methods=["post"], url_path="regenerate-qr")
    def regenerate_qr(self, request: Request, pk: str | None = None) -> Response:
        elevator = self.get_object()
        regenerate_qr_token(elevator)
        return Response(ElevatorDetailSerializer(elevator).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "token",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="The twelve-character token printed on the label.",
            )
        ],
        responses={200: ElevatorByQrSerializer},
    )
    @action(
        detail=False,
        methods=["get"],
        url_path=r"by-qr/(?P<token>[^/.]+)",
        permission_classes=[RolePermission],
    )
    def by_qr(self, request: Request, token: str | None = None) -> Response:
        # Scoped through the normal queryset, so another company's token is a
        # 404 rather than a 403 — a 403 would confirm the sticker is real and
        # let someone map a competitor's estate by trying tokens.
        elevator = self.get_queryset().filter(qr_token=token).first()
        if elevator is None:
            from rest_framework.exceptions import NotFound

            raise NotFound
        return Response(ElevatorByQrSerializer(elevator).data)

    @extend_schema(
        request=LabelRequestSerializer,
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
        description=(
            "A printable A4 sheet, twelve labels to the page. The identifiers "
            "are sent in the body rather than the query string: a firm printing "
            "its whole estate would otherwise build a URL no proxy will accept."
        ),
    )
    @action(detail=False, methods=["post"], url_path="labels")
    def labels(self, request: Request) -> HttpResponse:
        form = LabelRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        wanted = form.validated_data["elevator_ids"]

        # Through the normal queryset, so an id from another company simply is
        # not here. The order given by the caller is preserved, because the user
        # picked it and a sheet that reshuffles itself is hard to check against
        # the screen it came from.
        found = {
            elevator.id: elevator
            for elevator in self.get_queryset().filter(id__in=wanted).select_related("building")
        }
        elevators = [found[one] for one in wanted if one in found]
        if not elevators:
            raise NotFound

        try:
            pdf = render_labels(elevators, request.user.company)
        except PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc

        response = HttpResponse(pdf, content_type="application/pdf")
        # inline, not attachment: the whole point is to reach a print dialogue,
        # and a download the user then has to find and open is a step nobody
        # standing next to a printer wants.
        response["Content-Disposition"] = 'inline; filename="qr-labels.pdf"'
        return response
