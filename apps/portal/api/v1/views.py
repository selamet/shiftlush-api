"""The customer portal's API, and the office's handful of endpoints to run it.

M6: docs/superpowers/specs/2026-10-21-m6-customer-portal-design.md.

Three kinds of view live here and they never share an authentication class:

- the portal's anonymous auth endpoints (no authentication at all);
- the portal itself, behind PortalJWTAuthentication only (apps.portal.viewsets);
- the office's invitations and accounts, behind the staff authentication and
  the permission matrix like every other staff endpoint.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.conf import settings
from django.db.models import Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.attachments.api.v1.serializers import DownloadUrlSerializer
from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.attachments.services import download_url
from apps.customers.models import Customer, CustomerContact
from apps.elevators.models import Elevator
from apps.faults import services as fault_services
from apps.faults.models import Fault
from apps.inspections.models import Inspection, NonConformity
from apps.inspections.services import with_open_correction
from apps.maintenance.models import MaintenanceVisit
from apps.portal import services
from apps.portal.api.v1.serializers import (
    PortalAcceptSerializer,
    PortalAccountSerializer,
    PortalElevatorSerializer,
    PortalFaultReportSerializer,
    PortalFaultSerializer,
    PortalInspectionSerializer,
    PortalInvitationSerializer,
    PortalLoginSerializer,
    PortalMeSerializer,
    PortalPasswordResetConfirmSerializer,
    PortalPasswordResetRequestSerializer,
    PortalTokenResponseSerializer,
    PortalVisitSerializer,
)
from apps.portal.models import PortalAccount
from apps.portal.viewsets import PortalContextMixin, PortalViewSet, portal_account
from core.client_ip import client_ip
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.filters import TenantFilterSet
from core.idempotency import ReplayProtectedCreate, replay_protected
from core.permissions import RolePermission

#: The portal's refresh cookie, its own name and its own path: the staff cookie
#: is scoped to /api/v1/auth, so neither is ever sent to the other's endpoints.
REFRESH_COOKIE = "portal_refresh"
REFRESH_COOKIE_PATH = "/api/v1/portal/"


def _token_response(pair: services.TokenPair, account: PortalAccount, code: int) -> Response:
    response = Response(
        PortalTokenResponseSerializer({"access": pair.access, "account": account}).data,
        status=code,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh,
        expires=pair.refresh_expires_at,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path=REFRESH_COOKIE_PATH,
    )
    return response


class _Anonymous(APIView):
    """The portal's sign-in endpoints: nobody is signed in yet.

    No authentication class at all, so a stale staff or portal token in the
    browser cannot turn a sign-in into a 401. Throttled by the API-wide default,
    which for an anonymous caller is the same per-address limit the staff
    sign-in endpoints have.
    """

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []


class PortalLoginView(_Anonymous):
    """Sign in with e-mail and password; the staff lockout rules apply."""

    @extend_schema(request=PortalLoginSerializer, responses={200: PortalTokenResponseSerializer})
    def post(self, request: Request) -> Response:
        form = PortalLoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        caller = client_ip(request)
        account = services.authenticate(**form.validated_data, ip=caller)
        pair = services.issue_tokens(
            account, user_agent=request.headers.get("User-Agent", ""), ip=caller
        )
        return _token_response(pair, account, status.HTTP_200_OK)


class PortalRefreshView(_Anonymous):
    """Exchange the `portal_refresh` cookie for a new access token; rotates."""

    @extend_schema(request=None, responses={200: PortalTokenResponseSerializer})
    def post(self, request: Request) -> Response:
        token = request.COOKIES.get(REFRESH_COOKIE)
        if not token:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        account, pair = services.rotate_refresh_token(
            refresh_token=token,
            user_agent=request.headers.get("User-Agent", ""),
            ip=client_ip(request),
        )
        return _token_response(pair, account, status.HTTP_200_OK)


class PortalLogoutView(_Anonymous):
    """End this session and clear the `portal_refresh` cookie."""

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        token = request.COOKIES.get(REFRESH_COOKIE)
        if token:
            services.revoke_refresh_token(token)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
        return response


class PortalAcceptView(_Anonymous):
    """Accept an invitation: set a password and sign in."""

    @extend_schema(request=PortalAcceptSerializer, responses={201: PortalTokenResponseSerializer})
    def post(self, request: Request) -> Response:
        form = PortalAcceptSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        account = services.accept(
            token=form.validated_data["token"], password=form.validated_data["password"]
        )
        pair = services.issue_tokens(
            account, user_agent=request.headers.get("User-Agent", ""), ip=client_ip(request)
        )
        return _token_response(pair, account, status.HTTP_201_CREATED)


class PortalPasswordResetView(_Anonymous):
    """Mail a reset link. 202 whether or not the address has an account."""

    @extend_schema(request=PortalPasswordResetRequestSerializer, responses={202: None})
    def post(self, request: Request) -> Response:
        form = PortalPasswordResetRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        services.request_password_reset(form.validated_data["email"])
        # 202 whether or not the address has an account.
        return Response(status=status.HTTP_202_ACCEPTED)


class PortalPasswordResetConfirmView(_Anonymous):
    """Set a new password from the mailed link; every session ends."""

    @extend_schema(request=PortalPasswordResetConfirmSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        form = PortalPasswordResetConfirmSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        services.confirm_password_reset(
            token=form.validated_data["token"],
            new_password=form.validated_data["password"],
            ip=client_ip(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PortalMeView(PortalContextMixin):
    @extend_schema(responses={200: PortalMeSerializer})
    def get(self, request: Request) -> Response:
        """Who is signed in, for whom, and whom to call."""
        return Response(PortalMeSerializer(portal_account(request)).data)


# What a contact reads ---------------------------------------------------------


class PortalElevatorViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, PortalViewSet[Elevator]
):
    queryset = Elevator.objects.none()
    serializer_class = PortalElevatorSerializer
    customer_path = "building__customer_id"
    ordering = ["name", "id"]

    def get_base_queryset(self) -> QuerySet[Elevator]:
        return with_open_correction(Elevator.objects.select_related("building"))


class PortalVisitFilter(TenantFilterSet):
    class Meta:
        model = MaintenanceVisit
        fields = ["elevator"]


class PortalVisitViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, PortalViewSet[MaintenanceVisit]
):
    queryset = MaintenanceVisit.objects.none()
    serializer_class = PortalVisitSerializer
    customer_path = "elevator__building__customer_id"
    filterset_class = PortalVisitFilter
    ordering = ["-visit_date", "-id"]

    def get_base_queryset(self) -> QuerySet[MaintenanceVisit]:
        return MaintenanceVisit.objects.select_related("elevator", "technician").prefetch_related(
            "items"
        )

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        # The visit photos are a polymorphic relation that cannot be joined, so
        # the files of every visit on the page are read in one query here and
        # handed in, rather than one query per visit inside the serializer.
        instance = args[0] if args else None
        if instance is None:
            visits: list[MaintenanceVisit] = []
        elif kwargs.get("many"):
            visits = list(instance)
        else:
            visits = [instance]
        files: defaultdict[Any, list[Attachment]] = defaultdict(list)
        if visits:
            for attachment in Attachment.objects.filter(
                object_type=ObjectType.MAINTENANCE_VISIT,
                category=AttachmentCategory.PHOTO,
                object_id__in=[visit.pk for visit in visits],
            ).order_by("created_at", "id"):
                files[attachment.object_id].append(attachment)
        kwargs.setdefault("context", self.get_serializer_context())
        kwargs["context"]["files"] = files
        if kwargs.get("many"):
            args = (visits, *args[1:])
        return super().get_serializer(*args, **kwargs)


class PortalInspectionFilter(TenantFilterSet):
    class Meta:
        model = Inspection
        fields = ["elevator"]


class PortalInspectionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, PortalViewSet[Inspection]
):
    queryset = Inspection.objects.none()
    serializer_class = PortalInspectionSerializer
    customer_path = "elevator__building__customer_id"
    filterset_class = PortalInspectionFilter
    ordering = ["-inspected_on", "-id"]

    def get_base_queryset(self) -> QuerySet[Inspection]:
        return Inspection.objects.select_related("elevator").prefetch_related(
            Prefetch("nonconformities", queryset=NonConformity.objects.order_by("created_at", "id"))
        )


class PortalFaultFilter(TenantFilterSet):
    class Meta:
        model = Fault
        fields = ["elevator", "status"]


class PortalFaultViewSet(
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    PortalViewSet[Fault],
):
    """The customer's faults — the contact's own and everybody else's at the
    customer — and reporting a new one."""

    queryset = Fault.objects.none()
    serializer_class = PortalFaultSerializer
    customer_path = "elevator__building__customer_id"
    filterset_class = PortalFaultFilter
    ordering = ["-reported_at", "-id"]

    def get_base_queryset(self) -> QuerySet[Fault]:
        return Fault.objects.select_related("elevator__building", "technician")

    def get_serializer_context(self) -> dict[str, Any]:
        context = dict(super().get_serializer_context())
        if isinstance(self.request.user, PortalAccount):
            context["account"] = self.request.user
        return context

    @extend_schema(request=PortalFaultReportSerializer, responses={201: PortalFaultSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Report a fault on one of the customer's lifts.

        The public QR report's fields and priority mapping: `someone_trapped`
        makes it an emergency, `elevator_stopped` high, otherwise normal. Raises
        the same notifications as any report. Replay-protected by
        `Idempotency-Key`, keyed by this portal account.
        """
        account = portal_account(request)
        form = PortalFaultReportSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        data = form.validated_data
        fault = fault_services.report_by_portal(
            account=account,
            elevator=data["elevator"],
            report=fault_services.PublicReport(
                description=data["description"],
                someone_trapped=data["someone_trapped"],
                elevator_stopped=data["elevator_stopped"],
            ),
        )
        fresh = self.get_base_queryset().get(pk=fault.pk)
        return Response(
            PortalFaultSerializer(fresh, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


def files_of_customer(customer_id: Any) -> Q:
    """The files a contact may download: the customer's visit photos,
    inspection reports and fault photos. Nothing filed on the lift itself
    (certificates, permits) and nothing on the contract."""
    visits = MaintenanceVisit.objects.filter(elevator__building__customer_id=customer_id)
    inspections = Inspection.objects.filter(elevator__building__customer_id=customer_id)
    faults = Fault.objects.filter(elevator__building__customer_id=customer_id)
    return (
        Q(
            object_type=ObjectType.MAINTENANCE_VISIT,
            category=AttachmentCategory.PHOTO,
            object_id__in=visits.values("id"),
        )
        | Q(
            object_type=ObjectType.INSPECTION,
            category=AttachmentCategory.INSPECTION_REPORT,
            object_id__in=inspections.values("id"),
        )
        | Q(
            object_type=ObjectType.FAULT,
            category=AttachmentCategory.PHOTO,
            object_id__in=faults.values("id"),
        )
    )


class PortalFileViewSet(PortalContextMixin, GenericViewSet[Attachment]):
    """Signed download URLs, for the customer's own files only.

    The same signed-URL flow as the staff endpoint; what differs is the set of
    files: anything outside the customer's scope is a 404, as if it did not
    exist.
    """

    queryset = Attachment.objects.none()
    serializer_class = DownloadUrlSerializer

    def get_queryset(self) -> QuerySet[Attachment]:
        user = self.request.user
        if not isinstance(user, PortalAccount):
            return Attachment.objects.none()
        return Attachment.objects.filter(files_of_customer(user.customer_id))

    @extend_schema(request=None, responses={200: DownloadUrlSerializer})
    @action(detail=True, methods=["get"], url_path="download-url")
    def download_url(self, request: Request, pk: str | None = None) -> Response:
        attachment = self.get_object()
        return Response(
            DownloadUrlSerializer(
                {
                    "download_url": download_url(attachment),
                    "expires_in": settings.DOWNLOAD_URL_TTL_SECONDS,
                }
            ).data
        )


# The office's side ----------------------------------------------------------------


class PortalInvitationView(APIView):
    """`POST /customers/{id}/portal-invitations/` — invite a contact."""

    resource = "portal_access"
    permission_classes = [RolePermission]

    @extend_schema(request=PortalInvitationSerializer, responses={201: PortalAccountSerializer})
    @replay_protected
    def post(self, request: Request, customer_id: str) -> Response:
        customer = get_object_or_404(Customer.objects.select_related("company"), pk=customer_id)
        form = PortalInvitationSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        # Tenant-scoped: a contact of another firm is not found, and is refused
        # exactly like a contact of another customer of this one.
        contact = CustomerContact.objects.filter(pk=form.validated_data["contact"]).first()
        if contact is None or contact.customer_id != customer.pk:
            raise BusinessRuleError(ErrorCode.CONTACT_NOT_OF_CUSTOMER)
        account, _ = services.invite(customer=customer, contact=contact)
        return Response(PortalAccountSerializer(account).data, status=status.HTTP_201_CREATED)


class PortalAccountListView(generics.ListAPIView[PortalAccount]):
    """`GET /customers/{id}/portal-accounts/` — the customer's accounts."""

    resource = "portal_access"
    permission_classes = [RolePermission]
    serializer_class = PortalAccountSerializer
    queryset = PortalAccount.objects.none()
    ordering = ["email", "id"]

    def get_queryset(self) -> QuerySet[PortalAccount]:
        customer = get_object_or_404(Customer.objects, pk=self.kwargs["customer_id"])
        return PortalAccount.objects.filter(customer=customer).select_related("contact")


class PortalAccountViewSet(GenericViewSet[PortalAccount]):
    """`POST /portal-accounts/{id}/deactivate/` — close an account."""

    resource = "portal_access"
    permission_classes = [RolePermission]
    serializer_class = PortalAccountSerializer
    queryset = PortalAccount.objects.none()

    def get_queryset(self) -> QuerySet[PortalAccount]:
        return PortalAccount.objects.select_related("contact")

    @extend_schema(request=None, responses={200: PortalAccountSerializer})
    @action(detail=True, methods=["post"])
    def deactivate(self, request: Request, pk: str | None = None) -> Response:
        """Close the account now: no sign-in, and its sessions end."""
        account = services.deactivate(self.get_object())
        return Response(PortalAccountSerializer(account).data)
