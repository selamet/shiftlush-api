"""Invoice drafts, for the accountant first.

No create and no delete: the daily job writes drafts, and a draft nobody wants
is cancelled, with a reason, and stays on the record.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import django_filters
from django.db.models import Exists, OuterRef, Prefetch, QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.billing import documents, services
from apps.billing.api.v1.serializers import (
    InvoiceCancelSerializer,
    InvoiceDraftEditSerializer,
    InvoiceDraftReadSerializer,
    InvoiceIssueSerializer,
    InvoiceLineEditSerializer,
    InvoiceLineSerializer,
    InvoiceManualLineSerializer,
    InvoiceMarkPaidSerializer,
)
from apps.billing.models import InvoiceDraft, InvoiceLine
from core.dates import add_months
from core.exceptions import ServiceUnavailable
from core.filters import TenantFilterSet
from core.permissions import RolePermission
from core.requests import authenticated_user


class InvoiceDraftFilter(TenantFilterSet):
    period = django_filters.CharFilter(
        method="filter_period",
        help_text='A month, "YYYY-MM": drafts whose period overlaps it.',
    )
    needs_price = django_filters.BooleanFilter(method="filter_needs_price")

    class Meta:
        model = InvoiceDraft
        fields = ["status", "customer", "contract", "period", "needs_price"]

    def filter_period(
        self, queryset: QuerySet[InvoiceDraft], name: str, value: str
    ) -> QuerySet[InvoiceDraft]:
        year, _, month = value.partition("-")
        if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
            raise serializers.ValidationError(
                {"period": serializers.ErrorDetail('Must be "YYYY-MM".', "invalid")}
            )
        first = date(int(year), int(month), 1)
        # Overlap: starts before the next month begins, ends on or after this
        # one begins.
        return queryset.filter(period_start__lt=add_months(first, 1), period_end__gte=first)

    def filter_needs_price(
        self, queryset: QuerySet[InvoiceDraft], name: str, value: bool
    ) -> QuerySet[InvoiceDraft]:
        unpriced = Exists(InvoiceLine.objects.filter(draft_id=OuterRef("pk"), needs_price=True))
        return queryset.filter(unpriced) if value else queryset.exclude(unpriced)


class InvoiceDraftViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet[InvoiceDraft],
):
    resource = "invoice"
    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = InvoiceDraft.objects.none()
    serializer_class = InvoiceDraftReadSerializer
    filterset_class = InvoiceDraftFilter
    ordering = ["-period_start", "-id"]
    ordering_fields = ["period_start", "total", "invoice_date"]

    def get_queryset(self) -> QuerySet[InvoiceDraft]:
        return InvoiceDraft.objects.select_related("contract", "customer").prefetch_related(
            Prefetch("lines", queryset=InvoiceLine.objects.order_by("position", "id"))
        )

    def _read(self, draft: InvoiceDraft) -> Response:
        fresh = self.get_queryset().get(pk=draft.pk)
        return Response(InvoiceDraftReadSerializer(fresh).data)

    @extend_schema(request=InvoiceDraftEditSerializer, responses={200: InvoiceDraftReadSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """The notes. Draft only."""
        draft = self.get_object()
        form = InvoiceDraftEditSerializer(draft, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        services.edit_draft(
            draft, changes=dict(form.validated_data), by=authenticated_user(request)
        )
        return self._read(draft)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        kwargs["partial"] = True
        return self.partial_update(request, *args, **kwargs)

    @extend_schema(request=InvoiceManualLineSerializer, responses={201: InvoiceLineSerializer})
    @action(detail=True, methods=["post"], url_path="lines", url_name="lines")
    def add_line(self, request: Request, pk: str | None = None) -> Response:
        """Add a manual line. Draft only; the totals recompute."""
        draft = self.get_object()
        form = InvoiceManualLineSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        line = services.add_line(draft, by=authenticated_user(request), **form.validated_data)
        return Response(InvoiceLineSerializer(line).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=InvoiceIssueSerializer, responses={200: InvoiceDraftReadSerializer})
    @action(detail=True, methods=["post"])
    def issue(self, request: Request, pk: str | None = None) -> Response:
        """Record the e-invoice's number and date, and freeze the draft."""
        form = InvoiceIssueSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        draft = services.issue(
            self.get_object(), by=authenticated_user(request), **form.validated_data
        )
        return self._read(draft)

    @extend_schema(request=InvoiceMarkPaidSerializer, responses={200: InvoiceDraftReadSerializer})
    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request: Request, pk: str | None = None) -> Response:
        """Record the payment date. From `issued` only."""
        form = InvoiceMarkPaidSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        draft = services.mark_paid(
            self.get_object(), by=authenticated_user(request), **form.validated_data
        )
        return self._read(draft)

    @extend_schema(request=InvoiceCancelSerializer, responses={200: InvoiceDraftReadSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        """Cancel a draft or an issued draft; its faults are released."""
        form = InvoiceCancelSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        draft = services.cancel(
            self.get_object(), reason=form.validated_data["reason"], by=authenticated_user(request)
        )
        return self._read(draft)

    @extend_schema(responses={(200, "text/csv"): OpenApiTypes.STR})
    @action(
        detail=False,
        methods=["get"],
        url_path="export.csv",
        url_name="export-csv",
        pagination_class=None,
    )
    def export_csv(self, request: Request) -> HttpResponse:
        """The filtered list as CSV: semicolons, UTF-8 with a BOM, decimal commas.

        Every draft the filters select, not one page of them: an export is the
        list the accountant is looking at, all of it.
        """
        drafts = self.filter_queryset(self.get_queryset())
        response = HttpResponse(
            documents.export_csv(drafts).encode("utf-8"), content_type="text/csv; charset=utf-8"
        )
        response["Content-Disposition"] = 'attachment; filename="fatura-taslaklari.csv"'
        return response

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY})
    @action(detail=True, methods=["get"])
    def pdf(self, request: Request, pk: str | None = None) -> HttpResponse:
        """One page; marked as a draft, not an invoice, until it is issued."""
        draft = self.get_object()
        try:
            pdf = documents.render_pdf(draft)
        except documents.PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="taslak-{draft.pk}.pdf"'
        return response


class InvoiceLineViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet[InvoiceLine],
):
    """One line of a draft: price it, correct it, or take it off. Draft only."""

    resource = "invoice"
    permission_classes = [RolePermission]
    http_method_names = ["get", "patch", "delete", "head", "options"]
    queryset = InvoiceLine.objects.none()
    serializer_class = InvoiceLineSerializer

    def get_queryset(self) -> QuerySet[InvoiceLine]:
        return InvoiceLine.objects.select_related("draft")

    @extend_schema(request=InvoiceLineEditSerializer, responses={200: InvoiceLineSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        line = self.get_object()
        form = InvoiceLineEditSerializer(line, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        services.edit_line(line, changes=dict(form.validated_data), by=authenticated_user(request))
        return Response(InvoiceLineSerializer(line).data)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        kwargs["partial"] = True
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        services.remove_line(self.get_object(), by=authenticated_user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)
