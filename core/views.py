"""Infrastructure endpoints.

Not part of the API contract and therefore not versioned: a load balancer does
not negotiate versions, and pinning these to v1 would mean a v2 cutover could
take the health check with it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse

from core import storage
from core.error_codes import ErrorCode


def health(request: HttpRequest) -> JsonResponse:
    """Liveness. Answers if the process is up, and touches nothing else.

    Deliberately does not check the database: a health check that fails when a
    dependency is down gets the container killed and restarted, which fixes
    nothing and removes the instance that could have served cached reads.
    """
    return JsonResponse({"status": "ok"})


def ready(request: HttpRequest) -> JsonResponse:
    """Readiness. Answers whether this instance can serve traffic.

    This one does check dependencies, because the right response to a broken
    database is to stop sending requests here — not to restart.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        healthy = False

    # Storage is a hard dependency, not a nicety: an instance that cannot reach
    # the bucket accepts uploads that then fail at the confirmation step, which
    # looks to the user like their file vanished.
    if storage.reachable(settings.DEFAULT_STORAGE_BACKEND):
        checks["storage"] = "ok"
    else:
        checks["storage"] = "unreachable"
        healthy = False

    # Reported, but never fatal. Reverse geocoding is the first feature to use
    # the cache and it degrades rather than fails without one — the lookups go
    # to the provider instead, which costs quota, not correctness. Taking the
    # instance out of rotation over that would be the wrong trade. It is still
    # worth answering, because the two ways this connection breaks are both
    # silent: the shared Redis refuses any key outside the `shiftlush:`
    # namespace, and the credential belongs to a server several other
    # applications also use.
    try:
        cache.set("ready-probe", "ok", timeout=10)
        checks["cache"] = "ok" if cache.get("ready-probe") == "ok" else "error: read-back"
    except Exception as exc:
        checks["cache"] = f"error: {exc.__class__.__name__}"

    return JsonResponse(
        {"status": "ready" if healthy else "not_ready", "checks": checks},
        status=200 if healthy else 503,
    )


def not_found(request: HttpRequest, exception: Exception) -> JsonResponse:
    """The 404 for a URL that matches no route at all.

    DRF's exception handler never sees these: resolution fails before any view
    is reached, so Django's own handler answers — with an HTML page, from an API
    that documents a JSON envelope for every other error. A client parsing the
    body gets a syntax error and reports "something went wrong" instead of "that
    URL does not exist".

    Most often it is a missing trailing slash, so the answer says so rather than
    leaving the reader to compare the URL character by character.
    """
    return JsonResponse(
        {
            "error": {
                "code": ErrorCode.NOT_FOUND.value,
                "request_id": getattr(request, "request_id", ""),
                "detail": (
                    "No route matches this URL. Router paths end in a slash; "
                    "the authentication endpoints do not."
                ),
            }
        },
        status=404,
    )


def server_error(request: HttpRequest) -> JsonResponse:
    """The 500 for a failure outside DRF's reach.

    Same reason: an HTML page from a JSON API is one more thing for a client to
    fail to parse on the worst day it has.
    """
    return JsonResponse(
        {
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "request_id": getattr(request, "request_id", ""),
            }
        },
        status=500,
    )
