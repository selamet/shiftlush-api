"""Request-scoped plumbing: a correlation id, and the tenant context."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

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
    """Binds the authenticated user's company for the duration of the request.

    The company is taken from the authenticated user, never from the request
    body or the query string. If a client could name its own tenant, the whole
    isolation layer would be advisory.
    """

    def __init__(self, get_response: RequestHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        company_id: uuid.UUID | None = None
        if user is not None and user.is_authenticated:
            company_id = getattr(user, "company_id", None)

        with company_context(company_id):
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
