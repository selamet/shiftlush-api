"""Request-scoped plumbing: a correlation id, and the tenant context."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from http import HTTPStatus

from django.http import HttpRequest, HttpResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from core.client_ip import client_ip
from core.context import RequestActor, actor_context, company_context
from core.throttling import STATE_ATTRIBUTE, RateLimitState

RequestHandler = Callable[[HttpRequest], HttpResponse]


#: What an incoming id has to look like to be honoured.
#:
#: The id is not decoration. It goes on the log record, onto a Sentry tag, and
#: into the body of a 500, and it is the handle support asks a user to read out.
#: Whatever arrives in the header is written by the caller, so the shape has to
#: be checked before any of that happens: a value with a newline in it forges a
#: second log line, and a value with no ceiling puts an arbitrary number of
#: bytes on every record of a request that repeats it.
#:
#: Hex, dashes and underscores, 8 to 64 characters. That accepts the two forms
#: actually in use — this middleware's own `uuid4().hex`, and the dashed UUID a
#: proxy writes — and nothing that can break a line or a search.
_WELL_FORMED_ID = re.compile(r"\A[0-9a-fA-F][0-9a-fA-F_-]{7,63}\Z")


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
        # An incoming id is honoured so a trace can span the hop in front of
        # this process — Caddy, or a server-side client — and the API. It is
        # replaced rather than rejected when it does not look like an id: the
        # caller wanted a request served, not a lecture about a header, and a
        # 400 here would turn a malformed trace id into a failed request.
        #
        # The browser is deliberately not among the callers that can set it.
        # `x-request-id` is not CORS-safelisted and is absent from
        # CORS_ALLOW_HEADERS, so the frontend can read the id but not name it;
        # the reason is written where that list is.
        incoming = request.headers.get(self.HEADER, "")
        honoured = incoming if _WELL_FORMED_ID.match(incoming) else ""
        request.request_id = honoured or uuid.uuid4().hex  # type: ignore[attr-defined]
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

    def _actor(self, request: HttpRequest) -> RequestActor:
        header = self._auth.get_header(request)  # type: ignore[arg-type]
        user_id = None
        if header is not None:
            raw = self._auth.get_raw_token(header)
            if raw is not None:
                try:
                    token = self._auth.get_validated_token(raw)
                    claim = token.get("user_id")
                    user_id = uuid.UUID(str(claim)) if claim else None
                except (InvalidToken, ValueError):
                    user_id = None

        # Resolved by core.client_ip rather than here, so the address in the
        # audit trail and the address the rate limit counts against are the same
        # address. It used to read the left-most X-Forwarded-For entry, which
        # the caller writes: an audit row could name whichever address the
        # person being recorded felt like naming.
        return RequestActor(
            user_id=user_id,
            ip_address=client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Both contexts are opened here so the audit trail can name the actor
        # from inside a signal handler, where nothing else has a request.
        with (
            company_context(self._company_from_token(request)),
            actor_context(self._actor(request)),
        ):
            return self.get_response(request)


class RateLimitHeaderMiddleware:
    """Reports the caller's remaining quota, as 8.9 requires.

    Middleware rather than something in the view layer, because the response
    that most needs these headers is the one no view produced: a 429 is raised
    inside DRF's throttle check and rendered by the exception handler. Here it
    is the same three headers on every answer, refusals included.

    `Retry-After` is written here too, on a refusal, and deliberately overwrites
    the one DRF's exception handler produced. Both mean "the moment the window
    frees a slot"; DRF formats it with `%d`, which truncates, so a client that
    waits exactly as long as it was told comes back a fraction of a second early
    and is refused again. Rounded up they cost a client one extra second and
    agree with `RateLimit-Reset`, which is the same instant reported twice and
    should not be two different numbers.

    Endpoints with no throttle leave no state behind and get no headers. That is
    the honest answer for `/health` and `/ready`, which are plain Django views
    outside DRF entirely and are not counted at all.
    """

    def __init__(self, get_response: RequestHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        state: RateLimitState | None = getattr(request, STATE_ATTRIBUTE, None)
        if state is not None:
            response["RateLimit-Limit"] = str(state.limit)
            response["RateLimit-Remaining"] = str(state.remaining)
            response["RateLimit-Reset"] = str(state.reset)
            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                response["Retry-After"] = str(state.reset)
        return response


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
