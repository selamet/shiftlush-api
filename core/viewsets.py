"""The shape every business endpoint shares.

Written once so the tenant filter, the permission check and the technician
narrowing cannot be forgotten in the ninth module. A viewset that inherits from
here and declares its `resource` gets all three; one that forgets the resource
is denied rather than opened, so forgetting breaks an endpoint in testing
instead of exposing one in production.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model, QuerySet
from rest_framework import mixins, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from apps.users.models import Role
from core.permissions import RolePermission


class TenantViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """CRUD over a company-owned model.

    PUT is not routed: partial update is the only update verb. A full replace
    means the client has to send every field it does not want cleared, and the
    moment the model grows a column, old clients start wiping it.
    """

    #: Key into the permission matrix. Required.
    resource: str
    #: Serializer used for reads.
    read_serializer_class: type[BaseSerializer]
    #: Serializer used for create and update. Separate on purpose — a single
    #: class juggling read_only is how a field like `company` stays writable.
    write_serializer_class: type[BaseSerializer]
    #: Lookup path from this model to Customer, for the technician narrowing.
    customer_path: str | None = None

    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_class(self) -> type[BaseSerializer]:
        if self.action in ("create", "update", "partial_update"):
            return self.write_serializer_class
        return self.read_serializer_class

    def get_base_queryset(self) -> QuerySet[Model]:
        """Subclasses override this, not get_queryset.

        Overriding get_queryset directly would drop the technician narrowing
        below, silently and without any test failing except one that checks the
        rows a technician actually sees.
        """
        return super().get_queryset()

    def get_queryset(self) -> QuerySet[Model]:
        # The manager has already applied the company filter; this narrows
        # further for the one role that does not see the whole company.
        queryset = self.get_base_queryset()
        user = self.request.user
        if getattr(user, "role", None) == Role.TECHNICIAN and self.customer_path:
            assigned = list(user.customer_assignments.values_list("customer_id", flat=True))
            queryset = queryset.filter(**{f"{self.customer_path}__in": assigned})
        return queryset

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # Respond with the read representation. The write serializer omits `id`
        # and every computed field, so answering with it would leave the client
        # unable to link to the thing it just created.
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        self.perform_create(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        instance = self.get_object()
        write = self.get_serializer(instance, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        self.perform_update(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data)

    def perform_create(self, serializer: BaseSerializer) -> None:
        # The company comes from the authenticated user, never from the payload.
        # If a client could name its own tenant the whole boundary would be
        # advisory.
        serializer.save(
            company_id=self.request.user.company_id,
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_update(self, serializer: BaseSerializer) -> None:
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance: Model) -> None:
        # Soft delete. The model's delete() marks the row; nothing is removed.
        instance.updated_by = self.request.user
        instance.save(update_fields=["updated_by", "updated_at"])
        instance.delete()


class ReadOnlyTenantViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    resource: str
    permission_classes = [RolePermission]


class ReferenceViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Global reference data — provinces, districts, neighbourhoods.

    Not tenant-scoped because it is not owned by anyone, and read-only because
    it is loaded from a dataset once a year rather than edited.
    """

    pagination_class = None

    def get_serializer_context(self) -> dict[str, Any]:
        context: dict[str, Any] = super().get_serializer_context()
        return context


def request_user_company(request: Request) -> Any:
    return getattr(request.user, "company_id", None)
