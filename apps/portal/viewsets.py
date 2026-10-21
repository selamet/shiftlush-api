"""The shape every portal endpoint shares.

The portal's TenantViewSet: written once so that the firm context, the customer
narrowing and the portal-only authentication cannot be forgotten in the fifth
endpoint. A viewset that inherits from here and declares its `customer_path`
gets all three; one that forgets the path is refused rather than opened.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, TypeVar

from django.db.models import Model, QuerySet
from rest_framework import permissions, viewsets
from rest_framework.exceptions import NotAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.portal.authentication import PortalJWTAuthentication
from apps.portal.models import PortalAccount
from core.context import company_context
from core.throttling import ScopedRateThrottle

ModelT = TypeVar("ModelT", bound=Model)


class IsPortalAccount(permissions.BasePermission):
    """Only a signed-in portal account. A staff user is not one, by type."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        return isinstance(request.user, PortalAccount)


def portal_account(request: Request) -> PortalAccount:
    account = request.user
    if not isinstance(account, PortalAccount):
        raise NotAuthenticated
    return account


class PortalContextMixin(APIView):
    """Run the handler in the account's firm.

    The staff middleware binds a firm from the staff token's claim. A portal
    token carries no firm claim on purpose — nothing staff-side may key on it —
    so the context is bound here, after authentication has said which account
    this is, and released however the request ends.
    """

    authentication_classes = [PortalJWTAuthentication]
    permission_classes = [IsPortalAccount]
    # Its own budget per account (PORTAL_RATE_LIMIT), instead of the staff
    # user limit. Declaring throttle_classes replaces the default rather than
    # adding to it.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "portal"

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        self._portal_context = ExitStack()
        self._portal_context.enter_context(company_context(portal_account(request).company_id))

    def finalize_response(
        self, request: Request, response: Response, *args: Any, **kwargs: Any
    ) -> Response:
        try:
            return super().finalize_response(request, response, *args, **kwargs)
        finally:
            stack = getattr(self, "_portal_context", None)
            if stack is not None:
                stack.close()


class PortalViewSet(PortalContextMixin, viewsets.GenericViewSet[ModelT]):
    """Read endpoints over one model, narrowed to the account's customer.

    Subclasses override `get_base_queryset`, never `get_queryset`, for the same
    reason TenantViewSet asks it: overriding the narrowing is how one customer
    ends up reading another's lifts.
    """

    #: Lookup path from this model to the Customer, e.g. "building__customer_id".
    customer_path: str | None = None

    def get_base_queryset(self) -> QuerySet[ModelT]:
        return super().get_queryset()

    def get_queryset(self) -> QuerySet[ModelT]:
        queryset = self.get_base_queryset()
        if self.customer_path is None:
            # Forgetting the path is an empty endpoint, not an open one.
            return queryset.none()
        user = self.request.user
        if not isinstance(user, PortalAccount):
            # The schema generator walks viewsets without a request user.
            return queryset.none()
        return queryset.filter(**{self.customer_path: user.customer_id})
