"""Managing colleagues: users, invitations and technician assignments."""

from __future__ import annotations

from typing import Any

import django_filters
from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.users import services
from apps.users.api.v1.team_serializers import (
    AssignedCustomersSerializer,
    InvitationCreateSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from apps.users.models import Invitation, Role, User
from core.context import require_current_company_id
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.idempotency import ReplayProtectedCreate
from core.permissions import RolePermission


class UserFilter(django_filters.FilterSet):
    class Meta:
        model = User
        fields = ["role", "is_active"]


class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet,
):
    """Colleagues.

    There is no create: accounts come from invitations, because an
    administrator who can set a password can read one, and the specification
    rules that out for both security and data-protection reasons. There is no
    delete either — a leaver is deactivated, since their audit trail has to
    outlive their employment.
    """

    resource = "user"
    # Specification 6.2 gives deactivation to the owner alone. It is a POST on
    # this viewset, so without this line it resolves to "user"/WRITE and an
    # administrator is let through — the rule was written down and never
    # enforced.
    resource_by_action = {"deactivate": "user_deactivation"}
    # Never served — get_queryset() below replaces it on every request. It is
    # here so the schema generator can find the model without running a
    # request-time code path, and it is `none()` rather than `all()` so that if
    # the override were ever lost the failure is an empty list rather than every
    # company's staff.
    queryset = User.objects.none()
    permission_classes = [RolePermission]
    filterset_class = UserFilter
    ordering = ["first_name", "last_name"]
    ordering_fields = ["first_name", "last_name", "role", "created_at"]
    http_method_names = ["get", "patch", "post", "put", "head", "options"]

    def get_queryset(self) -> QuerySet[User]:
        # The company filter is applied here, by hand, because User's manager is
        # deliberately *not* tenant-scoped: authentication has to find a user
        # before the company is known, since the company comes from the user.
        # That makes this line the only thing standing between one firm and
        # another firm's staff list — it is not an optimisation.
        return User.objects.filter(company_id=require_current_company_id()).prefetch_related(
            "customer_assignments"
        )

    def get_serializer_class(self) -> Any:
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        user = self.get_object()
        form = UserUpdateSerializer(user, data=request.data, partial=True)
        form.is_valid(raise_exception=True)

        role = form.validated_data.pop("role", None)
        if role == Role.OWNER and request.user.role != Role.OWNER:
            # The rule InvitationCreateSerializer already applies, on the other
            # path to the same outcome: an administrator who could mint an owner
            # could promote themselves past the one role they do not hold.
            # Checked before anything is written, so a refused request leaves
            # the record untouched rather than half-renamed.
            raise PermissionDenied()

        for field, value in form.validated_data.items():
            setattr(user, field, value)
        user.save()

        if role is not None:
            # Through the service, not by assignment: changing the last owner's
            # role locks everyone out of user management with no way back.
            services.change_role(user=user, role=role)

        return Response(UserSerializer(user).data)

    @extend_schema(request=None, responses={200: UserSerializer})
    @action(detail=True, methods=["post"])
    def deactivate(self, request: Request, pk: str | None = None) -> Response:
        user = self.get_object()
        if user.pk == request.user.pk:
            # Not a permission question — an owner may deactivate owners. It is
            # that doing it to yourself ends the session performing the action,
            # and if you were the last owner nobody can undo it.
            raise BusinessRuleError(ErrorCode.CANNOT_DEACTIVATE_SELF)

        services.deactivate_user(user=user)
        return Response(UserSerializer(user).data)

    @extend_schema(
        request=AssignedCustomersSerializer,
        responses={200: UserSerializer},
        description="Replace the set of customers this technician may see.",
    )
    @action(detail=True, methods=["put"])
    def customers(self, request: Request, pk: str | None = None) -> Response:
        user = self.get_object()
        form = AssignedCustomersSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        services.set_assigned_customers(
            user=user,
            customer_ids=form.validated_data["customer_ids"],
            assigned_by=request.user,
        )
        user.refresh_from_db()
        return Response(UserSerializer(user).data)


# Not a TenantViewSet: there is no update, and the create mints a token and
# sends a message rather than saving a row. That is why the replay protection
# has to be inherited explicitly here — without it this endpoint accepted
# Idempotency-Key the way any endpoint accepts any header, and ignored it. A
# duplicate here is worse than a duplicate row: each invitation mails its own
# live token and nothing invalidates the first, so the invitee ends up holding
# two working links for one seat.
class InvitationViewSet(
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet,
):
    """Pending and past invitations.

    Deleting one is how an invitation is revoked: the row is soft-deleted, and
    the token stops resolving because the lookup is scoped to live rows. A
    mistyped address should stop working the moment it is noticed.
    """

    resource = "invitation"
    serializer_class = InvitationSerializer
    permission_classes = [RolePermission]
    ordering = ["-created_at"]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self) -> QuerySet[Invitation]:
        return Invitation.objects.select_related("invited_by")

    @extend_schema(request=InvitationCreateSerializer, responses={201: InvitationSerializer})
    def create(self, request: Request) -> Response:
        # Until the address is proven, this account may enter data but may not
        # send mail to other people. An unverified account may have been opened
        # with an address its owner does not control, and an invitation is a
        # real message to a real person — see specification 7.1.
        if not request.user.is_email_verified:
            raise BusinessRuleError(ErrorCode.EMAIL_NOT_VERIFIED)

        form = InvitationCreateSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        invitation, _token = services.create_invitation(
            company=request.user.company, invited_by=request.user, **form.validated_data
        )
        # The token is deliberately dropped here. It exists once, in the e-mail;
        # returning it would put a working credential in an API response and in
        # every log that records one.
        return Response(InvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: InvitationSerializer})
    @action(detail=True, methods=["post"])
    def resend(self, request: Request, pk: str | None = None) -> Response:
        invitation = self.get_object()
        services.resend_invitation(invitation)
        return Response(InvitationSerializer(invitation).data)


class InvitationVerifyView(APIView):
    """What the sign-up screen reads before anyone has an account.

    Public by necessity: the invitee cannot authenticate yet. It answers only
    for a valid, unexpired, unaccepted token, and returns only what the e-mail
    they are holding already told them.
    """

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    @extend_schema(
        parameters=[OpenApiParameter("token", OpenApiTypes.STR, OpenApiParameter.PATH)],
        responses={200: InvitationPreviewSerializer},
    )
    def get(self, request: Request, token: str) -> Response:
        invitation = services.invitation_for_token(token)
        return Response(
            InvitationPreviewSerializer(
                {
                    "email": invitation.email,
                    "first_name": invitation.first_name,
                    "last_name": invitation.last_name,
                    "role": invitation.role,
                    "company_name": invitation.company.display_name,
                    "expires_at": invitation.expires_at,
                }
            ).data
        )
