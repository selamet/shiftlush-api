from __future__ import annotations

import django_filters
from django.db.models import QuerySet
from rest_framework import serializers
from rest_framework.viewsets import GenericViewSet, mixins

from apps.audit.models import AuditLog
from core.context import require_current_company_id
from core.permissions import RolePermission


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        # `id` is absent on purpose. The key is a bigserial — the deliberate
        # exception to the no-sequential-ids rule, because this table is high
        # volume and never addressed from outside — and returning it would leak
        # how many writes a company makes. Paging goes by created_at.
        fields = [
            "company_id",
            "user_id",
            "table_name",
            "record_id",
            "action",
            "old_values",
            "new_values",
            "ip_address",
            "created_at",
        ]
        read_only_fields = fields


class AuditLogFilter(django_filters.FilterSet):
    since = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    until = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = AuditLog
        fields = ["table_name", "record_id", "action", "user_id"]


class AuditLogViewSet(mixins.ListModelMixin, GenericViewSet):
    """Read-only, and only for owners and admins.

    There is no create, update or delete anywhere: a log that can be edited is
    not evidence. Writes reach this table through signals only.
    """

    resource = "audit_log"
    # Never served: get_queryset() below replaces it on every request. It exists
    # so the schema generator can find the model without executing a request-time
    # code path, and it is `none()` rather than `all()` so that if the override
    # were ever lost the failure is an empty list, not another company's trail.
    queryset = AuditLog.objects.none()
    serializer_class = AuditLogSerializer
    permission_classes = [RolePermission]
    filterset_class = AuditLogFilter
    ordering = ["-created_at"]

    def get_queryset(self) -> QuerySet[AuditLog]:
        # AuditLog is not a CompanyOwnedModel — it holds a plain UUID so an
        # entry survives the hard deletion of what it describes — so the tenant
        # filter is applied here by hand.
        return AuditLog.objects.filter(company_id=require_current_company_id())
