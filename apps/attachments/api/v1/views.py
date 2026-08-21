from __future__ import annotations

import django_filters
from django.conf import settings
from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.attachments.api.v1.serializers import (
    AttachmentConfirmSerializer,
    AttachmentSerializer,
    DownloadUrlSerializer,
    UploadUrlRequestSerializer,
    UploadUrlResponseSerializer,
)
from apps.attachments.models import Attachment, ObjectType
from apps.attachments.services import confirm_upload, download_url, prepare_upload
from apps.users.models import Role
from core.permissions import RolePermission


class AttachmentFilter(django_filters.FilterSet):
    class Meta:
        model = Attachment
        # The question this endpoint is nearly always asked: what is attached to
        # this record.
        fields = ["object_type", "object_id", "category"]


class AttachmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet,
):
    """Files, without the bytes.

    There is no update: a file is not edited, it is replaced. Correcting one
    means uploading the new version and deleting the old, which leaves both in
    the audit trail — where a silent overwrite would leave neither.
    """

    resource = "attachment"
    serializer_class = AttachmentSerializer
    permission_classes = [RolePermission]
    filterset_class = AttachmentFilter
    ordering = ["-created_at"]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self) -> QuerySet[Attachment]:
        queryset = Attachment.objects.select_related("uploaded_by")
        user = self.request.user
        if getattr(user, "role", None) == Role.TECHNICIAN:
            queryset = queryset.filter(self._visible_to_technician(user))
        return queryset

    @staticmethod
    def _visible_to_technician(user: object) -> Q:
        """A technician sees the files of the customers they are assigned to.

        The polymorphic pair cannot be joined, so each type is narrowed by its
        own subquery. Without this the tenant filter alone would show a
        technician every document in the firm, including contracts for customers
        they have never visited — the company boundary holds, the assignment
        boundary does not.
        """
        from apps.contracts.models import Contract
        from apps.elevators.models import Elevator
        from apps.properties.models import Building

        assigned = user.customer_assignments.values_list("customer_id", flat=True)  # type: ignore[attr-defined]
        return (
            Q(object_type=ObjectType.CUSTOMER, object_id__in=assigned)
            | Q(
                object_type=ObjectType.BUILDING,
                object_id__in=Building.objects.filter(customer_id__in=assigned).values("id"),
            )
            | Q(
                object_type=ObjectType.ELEVATOR,
                object_id__in=Elevator.objects.filter(building__customer_id__in=assigned).values(
                    "id"
                ),
            )
            | Q(
                object_type=ObjectType.CONTRACT,
                object_id__in=Contract.objects.filter(customer_id__in=assigned).values("id"),
            )
        )

    @extend_schema(
        request=UploadUrlRequestSerializer,
        responses={200: UploadUrlResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="upload-url")
    def upload_url(self, request: Request) -> Response:
        """Ask for permission to upload, then upload straight to storage.

        No record is created here. A row for a file that was never uploaded is
        worse than no row: it appears in lists, it cannot be downloaded, and
        nothing ever cleans it up.
        """
        form = UploadUrlRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        ticket = prepare_upload(company_id=request.user.company_id, **form.validated_data)
        return Response(UploadUrlResponseSerializer(ticket).data)

    @extend_schema(request=AttachmentConfirmSerializer, responses={201: AttachmentSerializer})
    def create(self, request: Request) -> Response:
        """Confirm an upload that has already landed.

        Not wrapped in replay protection: this call is idempotent by nature. A
        storage key identifies exactly one object, so confirming it twice returns
        the same record rather than creating a second one.
        """
        form = AttachmentConfirmSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        attachment = confirm_upload(
            company_id=request.user.company_id,
            uploaded_by=request.user,
            **form.validated_data,
        )
        return Response(AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: DownloadUrlSerializer})
    @action(detail=True, methods=["get"], url_path="download-url")
    def download_url(self, request: Request, pk: str | None = None) -> Response:
        """A URL that works for a few minutes and only for this file.

        The bucket is never public. Every read is a fresh signature, which is
        also why these URLs are not worth caching or embedding anywhere.
        """
        attachment = self.get_object()
        return Response(
            DownloadUrlSerializer(
                {
                    "download_url": download_url(attachment),
                    "expires_in": settings.DOWNLOAD_URL_TTL_SECONDS,
                }
            ).data
        )
