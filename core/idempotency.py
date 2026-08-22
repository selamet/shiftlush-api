"""Replay protection for POST requests.

A technician on a bad connection taps "save", sees nothing happen and taps
again. Without this, two contracts exist. Duplicate records from a retry are the
most common complaint in field software, and they are expensive to unpick
afterwards because both copies look legitimate.

The header is optional; clients that do not send it get today's behaviour.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response

from core.error_codes import ErrorCode
from core.exceptions import RecordInUse
from core.models import IdempotencyKey

HEADER = "Idempotency-Key"
RETENTION = timedelta(hours=24)


def _expired() -> QuerySet[IdempotencyKey]:
    return IdempotencyKey.objects.filter(expires_at__lte=timezone.now())


def count_expired_keys() -> int:
    return int(_expired().count())


def purge_expired_keys() -> int:
    """Delete every key whose window has closed.

    The wrapper below drops an expired row only when the same key is presented
    again, which is exactly the case that did not need cleaning up. A key used
    once — the overwhelming majority, because the header exists for a retry that
    usually never comes — is never looked at again and stays forever. Section
    5.15 asks for a daily command; this is what it calls.

    A hard delete: there is nothing here worth preserving once the window
    closes, and the table carries no soft-delete columns to preserve it with.
    """
    deleted, _ = _expired().delete()
    return int(deleted)


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

    @functools.wraps(handler)
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

    # Read back by ReplayProtectedCreate below, and by the test that walks the
    # URL configuration asking every create endpoint whether it is covered.
    wrapper.replay_protected = True  # type: ignore[attr-defined]
    return wrapper


def replay_exempt(handler: Callable[..., Response]) -> Callable[..., Response]:
    """Declare a create that needs no replay protection, and say so out loud.

    There is exactly one honest reason: the operation is already idempotent, so
    a retry converges on the same record without the header. Confirming an
    upload is that case — a storage key names one object.

    The marker exists so the exemption is a decision written next to the method
    rather than the absence of one, which is indistinguishable from an
    oversight.
    """
    handler.replay_exempt = True  # type: ignore[attr-defined]
    return handler


class ReplayProtectedCreate:
    """Protects whichever `create` a viewset actually ends up running.

    The decorator alone protects the method it is written on. A viewset that
    writes its own `create` — the usual reason being that the record is not a
    plain row — replaces that method with an unprotected one, and nothing about
    the class says so. The endpoint then accepts `Idempotency-Key` the way any
    endpoint accepts any header, and ignores it: the invisible failure, one
    viewset later.

    Hooking subclass creation turns "remember the decorator" into "declare the
    exemption". Anything that inherits this gets its create wrapped, including a
    subclass several levels down that overrides an already-protected one.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Only the class's own create: an inherited one is already wrapped, and
        # wrapping it a second time would store the same response twice.
        create = cls.__dict__.get("create")
        if create is None or not callable(create):
            return
        if getattr(create, "replay_protected", False) or getattr(create, "replay_exempt", False):
            return
        cls.create = replay_protected(create)
