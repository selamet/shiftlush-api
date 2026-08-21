"""Request-scoped plumbing: a correlation id, and the tenant context."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from core.context import company_context

RequestHandler = Callable[[HttpRequest], HttpResponse]


class RequestIDMiddleware:
    """Attaches an id to every request and echoes it back.

    The same id goes on every log line and into the body of a 500. When a user
    says "I got an error", reading that id off their screen turns a search
    through the day's logs into a single lookup.
    """

    HEADER = "X-Request-ID"

    def __init__(self, get_response: RequestHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # A client-supplied id is honoured so a trace can span the browser and
        # the API; one is generated when absent.
        incoming = request.headers.get(self.HEADER, "")
        request.request_id = incoming or uuid.uuid4().hex  # type: ignore[attr-defined]
        response = self.get_response(request)
        response[self.HEADER] = request.request_id  # type: ignore[attr-defined]
        return response


class CompanyContextMiddleware:
    """Binds the caller's company for the duration of the request.

    The company is read from the access token, never from the request body or
    the query string. If a client could name its own tenant the whole isolation
    layer would be advisory.

    It reads the token directly rather than `request.user`, because DRF
    authenticates inside the view — by the time JWTAuthentication has run,
    middleware is long past, and `request.user` here is still anonymous. That
    mismatch is easy to miss: every endpoint returns an empty list and looks
    like a permissions problem rather than a context one.

    The company id is a claim on the token, so this costs no query.
    """

    def __init__(self, get_response: RequestHandler) -> None:
        self.get_response = get_response
        self._auth = JWTAuthentication()

    def _company_from_token(self, request: HttpRequest) -> uuid.UUID | None:
        header = self._auth.get_header(request)  # type: ignore[arg-type]
        if header is None:
            return None
        raw = self._auth.get_raw_token(header)
        if raw is None:
            return None
        try:
            token = self._auth.get_validated_token(raw)
        except InvalidToken:
            # An invalid token is not this middleware's problem to report; the
            # view's authentication will reject it with the right status.
            return None
        claim = token.get("company_id")
        return uuid.UUID(claim) if claim else None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        with company_context(self._company_from_token(request)):
            return self.get_response(request)


class APIVersionHeaderMiddleware:
    """Reports which version answered.

    Without it, a client pinned to a version has no way to notice it is being
    served by another one, and neither do the logs.
    """

    def __init__(self, get_response: RequestHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        version = getattr(request, "version", None)
        if version:
            response["X-API-Version"] = version
        return response
