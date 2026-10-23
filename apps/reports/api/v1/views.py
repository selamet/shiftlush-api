"""The monthly customer report, for the office.

Reports are made by the monthly job; nobody creates or edits one here. The
office reads them, sends one again, previews any month and, owner and admin,
renders one again.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.attachments.api.v1.serializers import DownloadUrlSerializer
from apps.attachments.api.v1.views import download_url_response
from apps.reports import content, documents, services
from apps.reports.api.v1.serializers import CustomerReportReadSerializer, ReportPreviewSerializer
from apps.reports.models import CustomerReport
from core.exceptions import ServiceUnavailable
from core.filters import TenantFilterSet
from core.idempotency import replay_protected
from core.permissions import RolePermission
from core.requests import authenticated_user
from core.scheduling import local_today


class CustomerReportFilter(TenantFilterSet):
    class Meta:
        model = CustomerReport
        fields = ["customer", "period", "delivery"]


class CustomerReportViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet[CustomerReport]
):
    resource = "customer_report"
    resource_by_action = {"regenerate": "customer_report_regenerate"}
    permission_classes = [RolePermission]
    queryset = CustomerReport.objects.none()
    serializer_class = CustomerReportReadSerializer
    filterset_class = CustomerReportFilter
    ordering = ["-period", "customer__legal_name", "id"]
    ordering_fields = ["period", "generated_at"]
    # Sending mails the customer; a retried request must not mail twice.
    idempotency_key_required = True

    def get_queryset(self) -> QuerySet[CustomerReport]:
        return CustomerReport.objects.select_related("customer")

    def _read(self, report: CustomerReport) -> Response:
        fresh = self.get_queryset().get(pk=report.pk)
        return Response(self.get_serializer(fresh).data)

    @extend_schema(request=None, responses={200: CustomerReportReadSerializer})
    @action(detail=True, methods=["post"])
    @replay_protected
    def send(self, request: Request, pk: str | None = None) -> Response:
        """Send it, or send it again, to the customer's current recipients now."""
        report = self.get_object()
        # The mails go out when this commits. Outside a transaction they would
        # go at once, and a failure recorded as FAILED would be overwritten by
        # the SENT written after it.
        with transaction.atomic():
            try:
                services.send_report(report, by=authenticated_user(request))
            except documents.PdfRenderingUnavailable as exc:
                # Only when the file was missing and had to be rendered again.
                raise ServiceUnavailable() from exc
        return self._read(report)

    @extend_schema(responses={200: DownloadUrlSerializer})
    @action(detail=True, methods=["get"], url_path="download-url")
    def download_url(self, request: Request, pk: str | None = None) -> Response:
        """A short-lived URL for the report's PDF, as the attachments endpoint gives.

        Here so that whoever may read reports may read their file, without
        needing to read every attachment in the firm.
        """
        file = services.live_file(self.get_object())
        if file is None:
            raise NotFound()
        return download_url_response(file)

    @extend_schema(
        request=ReportPreviewSerializer, responses={(200, "application/pdf"): OpenApiTypes.BINARY}
    )
    @action(detail=False, methods=["post"])
    def preview(self, request: Request) -> HttpResponse:
        """The PDF of any month, the current one to date included. Stores nothing."""
        form = ReportPreviewSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = content.build_report(
            form.validated_data["customer"], form.validated_data["period"], today=local_today()
        )
        try:
            pdf = documents.render_pdf(data)
        except documents.PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="onizleme-{data.period}.pdf"'
        return response

    @extend_schema(request=None, responses={200: CustomerReportReadSerializer})
    @action(detail=True, methods=["post"])
    def regenerate(self, request: Request, pk: str | None = None) -> Response:
        """Render it again from the current record. The old file is kept, soft-deleted."""
        report = self.get_object()
        try:
            services.regenerate(report, today=local_today())
        except documents.PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc
        return self._read(report)
