"""Cross-origin access from the browser.

The frontend and the API are different origins on the same registrable domain,
so every call the application makes is cross-origin and the browser asks
permission first. Nothing else in this suite exercises that: Django's test
client, and any server-side HTTP client, ignore CORS entirely — which is exactly
how a header the browser refuses to send can pass every test and still break
every write in production.
"""

from __future__ import annotations

import pytest
from django.test import Client

ORIGIN = "https://shiftlush.selamet.dev"


@pytest.fixture
def browser(settings) -> Client:
    settings.CORS_ALLOWED_ORIGINS = [ORIGIN]
    return Client()


def preflight(client: Client, path: str, headers: str) -> dict[str, str]:
    response = client.options(
        path,
        HTTP_ORIGIN=ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
    )
    return {key.lower(): value for key, value in response.headers.items()}


class TestPreflight:
    def test_the_frontend_origin_is_allowed_with_credentials(self, browser, db):
        headers = preflight(browser, "/api/v1/auth/login", "content-type")

        assert headers["access-control-allow-origin"] == ORIGIN
        # The refresh token is an httpOnly cookie; without this the browser
        # sends the request but drops the cookie, and the session silently
        # never survives a reload.
        assert headers["access-control-allow-credentials"] == "true"

    def test_the_idempotency_header_is_permitted(self, browser, db):
        headers = preflight(browser, "/api/v1/customers/", "content-type,idempotency-key")
        allowed = headers["access-control-allow-headers"].lower()

        # Every create sends this. A custom header missing from the list makes
        # the browser refuse to send the request at all — reported to the user
        # as a CORS error, with the server never seeing a thing. Server-side
        # clients enforce no CORS, so this is invisible to every other test.
        assert "idempotency-key" in allowed

    def test_a_foreign_origin_gets_no_permission(self, browser, db):
        response = browser.options(
            "/api/v1/customers/",
            HTTP_ORIGIN="https://not-ours.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        )
        # Absent, not denied: the browser blocks a response that does not name
        # the origin, and naming it would be the whole permission.
        assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


class TestEveryHeaderTheClientSends:
    """The list has to cover what the client actually sends, not what looks right.

    CORS-safelisted headers need no permission; anything else does. This states
    the ones the frontend uses so adding a header to the client without adding
    it here fails a test rather than a user's save button.
    """

    SENT_BY_THE_CLIENT = ("authorization", "content-type", "idempotency-key")

    @pytest.mark.parametrize("header", SENT_BY_THE_CLIENT)
    def test_it_is_allowed(self, browser, db, header):
        headers = preflight(browser, "/api/v1/customers/", header)
        assert header in headers["access-control-allow-headers"].lower()
