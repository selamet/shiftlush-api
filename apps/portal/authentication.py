"""Portal authentication: portal tokens only.

The mirror of core.authentication.CompanyAwareJWTAuthentication, which refuses
every token carrying the portal audience. Between the two, a staff token never
opens a portal view and a portal token never opens a staff view, whatever the
view forgets to check.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken

from apps.portal.models import PortalAccount
from apps.portal.services import PORTAL_AUDIENCE


class PortalJWTAuthentication(JWTAuthentication):
    def get_validated_token(self, raw_token: bytes) -> Any:
        token = super().get_validated_token(raw_token)
        # Signature and expiry are checked above with the shared key; the
        # audience is what makes it a portal token, and nothing else will do.
        if token.get("aud") != PORTAL_AUDIENCE or "portal_account_id" not in token:
            raise InvalidToken("Not a portal token.")
        return token

    # The stubs type this as returning the staff user model; returning another
    # principal is the whole point of the class.
    def get_user(self, validated_token: Any) -> PortalAccount:  # type: ignore[override]
        try:
            account_id = UUID(str(validated_token["portal_account_id"]))
        except (KeyError, ValueError) as exc:
            raise InvalidToken("Token names no portal account.") from exc
        # `unscoped`: there is no tenant context yet — this is where the
        # request learns which firm it belongs to. The id is the token's own.
        account = (
            PortalAccount.unscoped.select_related("company", "customer", "contact")
            .filter(pk=account_id, is_deleted=False)
            .first()
        )
        if account is None:
            raise AuthenticationFailed("Portal account not found.", code="user_not_found")
        # A closed account, or one whose contact was deleted, stops working at
        # once rather than when its access token runs out.
        if not account.is_active or account.contact.is_deleted:
            raise AuthenticationFailed("Portal account is inactive.", code="user_inactive")
        return account
