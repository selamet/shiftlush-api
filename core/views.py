"""Infrastructure endpoints.

Not part of the API contract and therefore not versioned: a load balancer does
not negotiate versions, and pinning these to v1 would mean a v2 cutover could
take the health check with it.
"""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, JsonResponse


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

    return JsonResponse(
        {"status": "ready" if healthy else "not_ready", "checks": checks},
        status=200 if healthy else 503,
    )
