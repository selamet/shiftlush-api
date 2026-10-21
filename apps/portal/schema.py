"""The portal's bearer scheme in the published contract.

Its own name (`portalJwtAuth`) so a generated client can tell the two sessions
apart, and so no reader of the contract mistakes a portal endpoint for one a
staff token opens.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


# drf-spectacular registers extensions in an untyped __init_subclass__.
class PortalJWTScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "apps.portal.authentication.PortalJWTAuthentication"
    name = "portalJwtAuth"
    # Ahead of drf-spectacular's own simplejwt extension, which would otherwise
    # match this subclass of JWTAuthentication and call it `jwtAuth`.
    priority = 1

    def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "A customer portal access token (audience `portal`).",
        }
