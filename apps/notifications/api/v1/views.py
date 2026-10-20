"""The signed-in user's own notifications.

Not a TenantViewSet and not on the role matrix: every role may read its own
notifications, and nobody may read anyone else's — including an owner, who
administers the firm but does not read a colleague's inbox. So the permission
is plain IsAuthenticated, and the boundary is the queryset: the tenant manager
narrows to the company, and `recipient=request.user` narrows to the person. A
notification addressed to somebody else is a 404, exactly like one that never
existed.

Nothing here creates a record — notifications are raised by notify(), never by
a client — so there is no `create` for replay protection to wrap. The two POSTs
converge by themselves: marking something read twice leaves it read, with the
first timestamp.
"""

from __future__ import annotations

import django_filters
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.notifications import services
from apps.notifications.api.v1.serializers import (
    NotificationPreferenceSerializer,
    NotificationSerializer,
    UnreadCountSerializer,
    UpdatedCountSerializer,
)
from apps.notifications.models import Notification
from core.filters import TenantFilterSet
from core.requests import authenticated_company_id, authenticated_user


class NotificationFilter(TenantFilterSet):
    unread = django_filters.BooleanFilter(method="filter_unread")

    class Meta:
        model = Notification
        fields = ["unread", "kind"]

    def filter_unread(
        self, queryset: QuerySet[Notification], name: str, value: bool
    ) -> QuerySet[Notification]:
        # django-filter never calls this for an absent or blank value, so the
        # parameter is always a real answer: `true` is unread, `false` is read.
        return queryset.filter(read_at__isnull=value)


class NotificationViewSet(mixins.ListModelMixin, GenericViewSet[Notification]):
    # Never served: get_queryset() replaces it on every request. It is here so
    # the schema generator finds the model, and it is `none()` so that losing
    # the override would empty the list rather than open everybody's inbox.
    queryset = Notification.objects.none()
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = NotificationFilter
    # Newest first; the id breaks a tie between two raised in the same instant,
    # which a job raising one per recipient does all the time.
    ordering = ["-created_at", "-id"]
    ordering_fields = ["created_at"]

    def get_queryset(self) -> QuerySet[Notification]:
        return Notification.objects.filter(recipient=authenticated_user(self.request))

    @extend_schema(responses={200: UnreadCountSerializer})
    @action(detail=False, methods=["get"], url_path="unread-count", pagination_class=None)
    def unread_count(self, request: Request) -> Response:
        """How many of the caller's notifications are unread, for the bell.

        Its own endpoint rather than a field on the list, because the bell asks
        every minute from every open tab and needs one indexed count, not a page.
        """
        count = self.get_queryset().filter(read_at__isnull=True).count()
        return Response(UnreadCountSerializer({"count": count}).data)

    @extend_schema(request=None, responses={200: NotificationSerializer})
    @action(detail=True, methods=["post"])
    def read(self, request: Request, pk: str | None = None) -> Response:
        """Mark one notification read. Idempotent: the first read time is kept."""
        notification = self.get_object()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at", "updated_at"])
        return Response(self.get_serializer(notification).data)

    @extend_schema(request=None, responses={200: UpdatedCountSerializer})
    @action(detail=False, methods=["post"], url_path="read-all", pagination_class=None)
    def read_all(self, request: Request) -> Response:
        """Mark every unread notification of the caller's read.

        Answers with how many changed, so a client can tell "nothing to do"
        from "done" without asking for the count again. A queryset update, so
        no signal fires — which is fine, Notification is not audited.
        """
        now = timezone.now()
        count = self.get_queryset().filter(read_at__isnull=True).update(read_at=now, updated_at=now)
        return Response(UpdatedCountSerializer({"count": count}).data)

    @extend_schema(methods=["GET"], responses={200: NotificationPreferenceSerializer(many=True)})
    @extend_schema(
        methods=["PUT"],
        request=NotificationPreferenceSerializer(many=True),
        responses={200: NotificationPreferenceSerializer(many=True)},
    )
    @action(detail=False, methods=["get", "put"], pagination_class=None, filter_backends=[])
    def preferences(self, request: Request) -> Response:
        """Every kind with the caller's e-mail flag; PUT replaces them.

        GET always lists every kind, defaults filled in, so the settings screen
        never has to know what the defaults are. PUT takes the same list back;
        a kind left out returns to its default, and an unknown kind is a 400.
        """
        user = authenticated_user(request)
        # A user with no firm (a bare superuser) has nowhere to keep a choice;
        # 403 here rather than a constraint error on the first PUT.
        authenticated_company_id(request)
        if request.method == "PUT":
            form = NotificationPreferenceSerializer(data=request.data, many=True)
            form.is_valid(raise_exception=True)
            entries = services.set_preferences(user=user, entries=form.validated_data)
        else:
            entries = services.preferences_for(user)
        return Response(NotificationPreferenceSerializer(entries, many=True).data)
