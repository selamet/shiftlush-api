"""Authentication endpoints.

Views serialise, authorise and shape the response. Every rule that touches more
than one row lives in apps.users.services, so phase 2's mobile API calls the
same code rather than a second copy that drifts.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users import services
from apps.users.api.v1.serializers import (
    CurrentUserSerializer,
    EmailVerifySerializer,
    InvitationAcceptSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    TokenResponseSerializer,
)
from core.client_ip import client_ip
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError
from core.requests import authenticated_user

REFRESH_COOKIE = "shiftlush_refresh"


def _token_response(request: Request, pair: services.TokenPair, user: Any) -> Response:
    response = Response(
        TokenResponseSerializer({"access": pair.access, "user": user}).data,
        status=status.HTTP_200_OK,
    )
    # httpOnly so no script can read it, Secure outside development, and Lax
    # rather than None because the two hosts share a registrable domain in
    # production. SameSite=None would widen the CSRF surface and is blocked
    # outright by some browser settings.
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh,
        expires=pair.refresh_expires_at,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path="/api/v1/auth",
    )
    return response


class RegisterView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=RegisterSerializer, responses={201: TokenResponseSerializer})
    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _, owner = services.register_company(**serializer.validated_data)
        pair = services.issue_tokens(
            owner, user_agent=request.headers.get("User-Agent", ""), ip=client_ip(request)
        )
        response = _token_response(request, pair, owner)
        response.status_code = status.HTTP_201_CREATED
        return response


class LoginView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=LoginSerializer, responses={200: TokenResponseSerializer})
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # The lockout of 7.4 is counted per (e-mail, address), so the address is
        # an argument to the rule rather than something the service could look
        # up for itself. `core.client_ip` is the only place that answers it.
        caller = client_ip(request)
        user = services.authenticate(**serializer.validated_data, ip=caller)
        pair = services.issue_tokens(
            user, user_agent=request.headers.get("User-Agent", ""), ip=caller
        )
        return _token_response(request, pair, user)


class RefreshView(APIView):
    """Exchanges the refresh cookie for a new access token.

    Open to unauthenticated callers by definition: the access token has expired,
    which is why the client is here.
    """

    permission_classes = [AllowAny]

    @extend_schema(request=None, responses={200: TokenResponseSerializer})
    def post(self, request: Request) -> Response:
        token = request.COOKIES.get(REFRESH_COOKIE)
        if not token:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        pair = services.rotate_refresh_token(
            refresh_token=token,
            user_agent=request.headers.get("User-Agent", ""),
            ip=client_ip(request),
        )
        from apps.users.models import RefreshSession  # local import avoids a cycle

        user = RefreshSession.objects.get(token_hash=services._hash(pair.refresh)).user
        return _token_response(request, pair, user)


class LogoutView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        token = request.COOKIES.get(REFRESH_COOKIE)
        if token:
            services.revoke_refresh_token(token)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
        return response


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=PasswordResetRequestSerializer, responses={202: None})
    def post(self, request: Request) -> Response:
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.request_password_reset(serializer.validated_data["email"])
        # 202 whether or not the address exists. Any difference in the response
        # would turn this into a way to find out who has an account.
        return Response(status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=PasswordResetConfirmSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.confirm_password_reset(
            token=serializer.validated_data["token"],
            new_password=serializer.validated_data["password"],
            # Releases this address's lock, so somebody who was locked out is not
            # made to wait out a window they have just proved they should not be
            # in.
            ip=client_ip(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class EmailVerifyView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=EmailVerifySerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        serializer = EmailVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.confirm_email_verification(serializer.validated_data["token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvitationAcceptView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=InvitationAcceptSerializer, responses={201: TokenResponseSerializer})
    def post(self, request: Request) -> Response:
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.accept_invitation(
            token=serializer.validated_data["token"],
            password=serializer.validated_data["password"],
        )
        pair = services.issue_tokens(
            user, user_agent=request.headers.get("User-Agent", ""), ip=client_ip(request)
        )
        response = _token_response(request, pair, user)
        response.status_code = status.HTTP_201_CREATED
        return response


class EmailVerifyResendView(APIView):
    """Send the verification link again.

    The only way out for somebody who mistyped their address at registration or
    lost the message. Answers the same way whether or not there was anything to
    send: an authenticated caller asking about their own address learns nothing
    either way, and a verified account should not be told it is verified by a
    different status code.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        user = authenticated_user(request)
        if not user.is_email_verified:
            # Issuing a new token invalidates the previous one, which is what
            # should happen: two live links for one address is one more than
            # anybody needs.
            services.request_email_verification(user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: CurrentUserSerializer})
    def get(self, request: Request) -> Response:
        return Response(CurrentUserSerializer(authenticated_user(request)).data)
