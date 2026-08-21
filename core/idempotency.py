"""Replay protection for POST requests.

A technician on a bad connection taps "save", sees nothing happen and taps
again. Without this, two contracts exist. Duplicate records from a retry are the
most common complaint in field software, and they are expensive to unpick
afterwards because both copies look legitimate.

The header is optional; clients that do not send it get today's behaviour.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response

from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.models import IdempotencyKey

HEADER = "Idempotency-Key"
RETENTION = timedelta(hours=24)


def _fingerprint(request: Request) -> str:
    body = request.data if isinstance(request.data, dict | list) else {}
    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def replay_protected(handler: Callable[..., Response]) -> Callable[..., Response]:
    """Wrap a create view so the same key returns the same answer.

    The rule that matters: the same key with a *different* body is refused with
    409 rather than served the stored response. Returning the old answer there
    would reply to a question that was never asked, and that is one of the
    hardest classes of bug to trace afterwards.
    """

    def wrapper(self: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
        key = request.headers.get(HEADER, "").strip()
        if not key:
            return handler(self, request, *args, **kwargs)

        endpoint = f"{request.method} {request.path}"
        fingerprint = _fingerprint(request)

        existing = IdempotencyKey.objects.filter(
            company_id=request.user.company_id, user_id=request.user.pk, key=key
        ).first()

        if existing is not None:
            if existing.expires_at <= timezone.now():
                existing.delete()
            elif existing.request_hash != fingerprint or existing.endpoint != endpoint:
                raise RecordInUse(ErrorCode.IDEMPOTENCY_KEY_REUSED)
            else:
                return Response(existing.response_body, status=existing.response_status)

        response = handler(self, request, *args, **kwargs)

        # Only successful creates are worth replaying. Storing a failure would
        # make a transient error permanent for the next twenty-four hours.
        if 200 <= response.status_code < 300:
            response_data = response.data
            IdempotencyKey.objects.update_or_create(
                company_id=request.user.company_id,
                user_id=request.user.pk,
                key=key,
                defaults={
                    "endpoint": endpoint,
                    "request_hash": fingerprint,
                    "response_status": response.status_code,
                    "response_body": json.loads(json.dumps(response_data, default=str)),
                    "expires_at": timezone.now() + RETENTION,
                },
            )
        return response

    return wrapper
