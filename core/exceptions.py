"""One response shape for every error.

    {"error": {"code": "...", "request_id": "...", "details": [...]}}

No `message` field: the backend does not produce user-facing text. And no
success envelope either — the HTTP status carries that, so a client never has to
parse a body to find out whether the call worked.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from core.error_codes import ErrorCode

logger = logging.getLogger(__name__)


class BusinessRuleError(exceptions.APIException):
    """A request that is well-formed but not allowed by the domain.

    422 rather than 400: the payload is valid, the world is not in a state where
    it can be accepted. Clients treat the two differently — one means fix the
    form, the other means the situation changed.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY

    def __init__(self, code: ErrorCode, detail: str | None = None) -> None:
        super().__init__(detail or code.label, code=code.value)


class Conflict(exceptions.APIException):
    """The request cannot be applied to the world as it currently stands."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.label, code=code.value)


class RecordInUse(Conflict):
    def __init__(self, code: ErrorCode = ErrorCode.RECORD_IN_USE) -> None:
        super().__init__(code)


class DuplicateRecord(Conflict):
    """A business key this company already uses.

    409 rather than 400: the payload is well formed and would have been accepted
    a moment ago. What is wrong is the state of the world, and the client's
    remedy is to open the existing record rather than to fix the form.
    """


class ServiceUnavailable(exceptions.APIException):
    """A dependency this request needs is not available in this deployment.

    503 rather than 500: nothing about the request was wrong, and a client that
    retries later may well succeed. The distinction matters to whoever is
    reading the alert at three in the morning.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self, code: ErrorCode = ErrorCode.SERVICE_UNAVAILABLE) -> None:
        super().__init__(code.label, code=code.value)


class TenantIsolationError(Http404):
    """Another company's record.

    Deliberately a 404. A 403 would confirm the record exists, which is a small
    leak but a real one: it lets an outsider probe for valid identifiers and
    learn how many records a competitor has.
    """


def _field_details(detail: Any, prefix: str = "") -> list[dict[str, str]]:
    """Flatten DRF's nested error structure into a flat field/code list."""
    details: list[dict[str, str]] = []
    if isinstance(detail, dict):
        for field, value in detail.items():
            path = f"{prefix}.{field}" if prefix else str(field)
            details.extend(_field_details(value, path))
    elif isinstance(detail, list):
        for item in detail:
            details.extend(_field_details(item, prefix))
    else:
        code = getattr(detail, "code", None) or ErrorCode.VALIDATION_ERROR.value
        entry = {"code": str(code)}
        if prefix:
            entry["field"] = prefix
        details.append(entry)
    return details


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    request = context.get("request")
    request_id = getattr(request, "request_id", "") if request else ""

    # DRF converts Http404 too, but it does so on a local variable inside its
    # own handler — so the status came out as 404 while the body below still
    # read the original exception, found no `default_code` on it, and fell
    # through to INTERNAL_ERROR. Every "another company's record" answer in the
    # API said the server had broken rather than that the record was not there,
    # which tells a client to retry something that will never succeed. Converted
    # here, alongside the other three, so the code matches the status.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    # PROTECT raises this rather than deleting a row that others reference. The
    # message has to tell the user what to do, so the code is specific.
    elif isinstance(exc, ProtectedError):
        exc = RecordInUse()
    elif isinstance(exc, IntegrityError):
        # A last resort, not the intended path. Every constraint the API can
        # provoke is also checked above it, where the answer can name the field
        # — but a constraint reaching here used to surface as INTERNAL_ERROR,
        # which tells the client to retry something that will never succeed. The
        # detail stays out of the response and goes to the log: constraint names
        # describe the schema, and the schema is not the client's business.
        logger.warning("Constraint violation", extra={"request_id": request_id}, exc_info=exc)
        exc = Conflict(ErrorCode.CONSTRAINT_VIOLATION)
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Anything uncaught is a bug. The client gets the request id and nothing
        # else; the stack trace goes to the log, where it belongs.
        logger.exception("Unhandled exception", extra={"request_id": request_id})
        return Response(
            {"error": {"code": ErrorCode.INTERNAL_ERROR.value, "request_id": request_id}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        code = ErrorCode.VALIDATION_ERROR.value
        details = _field_details(exc.detail)
    else:
        code = str(getattr(exc, "default_code", "") or ErrorCode.INTERNAL_ERROR.value)
        explicit = getattr(exc, "detail", None)
        code = str(getattr(explicit, "code", None) or code)
        details = []

    body: dict[str, Any] = {"code": _map_code(exc, code), "request_id": request_id}
    if details:
        body["details"] = details

    response.data = {"error": body}
    return response


def _map_code(exc: Exception, fallback: str) -> str:
    """Translate DRF's generic codes into this project's vocabulary."""
    if isinstance(exc, exceptions.NotFound):
        return ErrorCode.NOT_FOUND.value
    if isinstance(exc, exceptions.PermissionDenied):
        return ErrorCode.PERMISSION_DENIED.value
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        return ErrorCode.AUTHENTICATION_FAILED.value
    if isinstance(exc, exceptions.Throttled):
        return ErrorCode.THROTTLED.value
    return fallback.upper()
