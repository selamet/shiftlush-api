"""What the office and the portal read of a monthly report."""

from __future__ import annotations

from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.customers.models import Customer
from apps.reports.models import CustomerReport
from apps.users.api.v1.serializers import StrictSerializer

PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class CustomerReportReadSerializer(serializers.ModelSerializer[CustomerReport]):
    """The office's view. The PDF downloads through the attachments' `download-url`."""

    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)

    class Meta:
        model = CustomerReport
        fields = [
            "id",
            "customer",
            "customer_name",
            "period",
            "file",
            "generated_at",
            "summary",
            "delivery",
            "sent_at",
            "recipients",
            "error",
        ]
        read_only_fields = fields


class PortalReportSerializer(serializers.ModelSerializer[CustomerReport]):
    """The contact's view: no delivery state, recipients or errors, which are the office's."""

    class Meta:
        model = CustomerReport
        fields = ["id", "period", "file", "generated_at", "summary"]
        read_only_fields = fields


@extend_schema_field(OpenApiTypes.UUID)
class _CustomerField(serializers.PrimaryKeyRelatedField[Customer]):
    """The firm's customers, looked up per request: the queryset is scoped by the
    firm bound at that moment, so it cannot be built once at import."""

    def get_queryset(self) -> QuerySet[Customer]:
        return Customer.objects.all()


class ReportPreviewSerializer(StrictSerializer):
    customer = _CustomerField()
    period = serializers.RegexField(PERIOD_PATTERN, help_text='A month, "YYYY-MM".')
