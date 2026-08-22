from __future__ import annotations

from typing import Any

import django_filters
from django.db.models import QuerySet
from rest_framework import serializers
from rest_framework.viewsets import GenericViewSet, mixins

from apps.audit.models import AuditLog
from apps.users.models import User
from core.context import require_current_company_id
from core.permissions import RolePermission


class AuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    def get_user_name(self, entry: AuditLog) -> str:
        """Who did it, by name.

        The names are looked up once for the whole page and handed in through
        the context — see the viewset. There is no foreign key to join on, and
        that is deliberate: the trail has to survive the hard deletion of what
        it describes, so it holds a plain id.

        Empty string rather than null for a write with no actor — a background
        job, a bootstrap flow — since the client prints this directly.
        """
        return str(self.context.get("user_names", {}).get(entry.user_id, ""))

    class Meta:
        model = AuditLog
        # `id` is absent on purpose. The key is a bigserial — the deliberate
        # exception to the no-sequential-ids rule, because this table is high
        # volume and never addressed from outside — and returning it would leak
        # how many writes a company makes. Paging goes by created_at.
        fields = [
            "company_id",
            "user_id",
            "user_name",
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

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        page = args[0] if args else None
        if page is not None and kwargs.get("many"):
            ids = {entry.user_id for entry in page if entry.user_id}
            # `unscoped` because the trail outlives employment: a leaver is
            # soft-deleted, and a tenant-scoped read would turn years of their
            # entries anonymous.
            kwargs.setdefault("context", self.get_serializer_context())
            kwargs["context"]["user_names"] = {
                user.pk: user.full_name for user in User.unscoped.filter(pk__in=ids)
            }
        return super().get_serializer(*args, **kwargs)

    def get_queryset(self) -> QuerySet[AuditLog]:
        # AuditLog is not a CompanyOwnedModel — it holds a plain UUID so an
        # entry survives the hard deletion of what it describes — so the tenant
        # filter is applied here by hand.
        return AuditLog.objects.filter(company_id=require_current_company_id())
