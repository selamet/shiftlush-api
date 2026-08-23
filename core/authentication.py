"""JWT authentication, with the company loaded alongside the user.

simplejwt fetches the user with a bare `get()`, so every view that then reads
`request.user.company` pays for a second query — and several do: the session
endpoint renders the firm's name, invitations are created against it, and QR
labels carry its logo and telephone number. The company is a foreign key on the
user's own row, and there is no request that has a user but not their company,
so the join is free in every sense that matters.

The join is added by lending simplejwt a different manager rather than by
reimplementing `get_user`. Its version also checks that the account is active
and, optionally, that the password has not changed since the token was issued.
Copying those checks here would mean silently losing the next one the library
adds, and a missing authentication check is not the kind of drift that shows up
in a test.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from rest_framework_simplejwt.authentication import JWTAuthentication


class _WithCompany:
    """The user model, but its default manager carries a join.

    Everything except `objects` is forwarded untouched — simplejwt also reads
    `DoesNotExist` off this object.
    """

    def __init__(self, model: type[Any]) -> None:
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    @property
    def objects(self) -> QuerySet[Any]:
        # A queryset, not a manager: simplejwt only ever calls `.get()` on this,
        # and `select_related` is what puts the join in. Saying Manager here
        # would have been a comfortable lie about what the caller receives.
        queryset: QuerySet[Any] = self._model.objects.select_related("company")
        return queryset


class CompanyAwareJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token: Any) -> Any:
        # DRF builds an authenticator per request, so this substitution is not
        # shared between requests; it is still restored, because an exception
        # raised by a later check must not leave the proxy in place.
        real_model = self.user_model
        self.user_model = _WithCompany(real_model)  # type: ignore[assignment]
        try:
            return super().get_user(validated_token)
        finally:
            self.user_model = real_model
