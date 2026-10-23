"""Replay protection for POST requests.

A technician on a bad connection taps "save", sees nothing happen and taps
again. Without this, two contracts exist. Duplicate records from a retry are the
most common complaint in field software, and they are expensive to unpick
afterwards because both copies look legitimate.

The header is optional; clients that do not send it get today's behaviour.

The key is reserved *before* the handler runs (issue #155). It used to be
stored only after the handler returned, so a client that timed out and retried
while the first request was still working found nothing and ran the handler a
second time — a second record, every report mail sent twice, and soon a second
collection booked against an invoice. Now:

- The first request inserts a pending row under the unique key, in its own
  transaction, and commits it before the work starts.
- An overlapping request with the same key finds that row and is refused with
  409 `IDEMPOTENCY_KEY_IN_FLIGHT`; the client retries later and is then served
  the stored answer.
- A successful handler fills the row in. A failed one (non-2xx, or an
  exception) deletes it, so a transient error neither sticks nor locks the key.
- A pending row older than `PENDING_TIMEOUT` belonged to a worker that died
  mid-request, and the next request takes it over — one winner, decided by a
  conditional update.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response

from core.error_codes import ErrorCode
from core.exceptions import BadRequest, Conflict, RecordInUse
from core.models import IdempotencyKey
from core.requests import authenticated_user

HEADER = "Idempotency-Key"
RETENTION = timedelta(hours=24)

#: How long a reservation may stay pending before it counts as abandoned. Well
#: past any request the API serves — the slowest is a report send over SMTP —
#: and short enough that a client whose first attempt died with its worker is
#: not shut out for long. Taking over a reservation whose handler is in fact
#: still running would run that handler twice, so this errs long.
PENDING_TIMEOUT = timedelta(seconds=60)

#: Insert, find the row gone (released in between), insert again. More rounds
#: than that means something other than this race is going on.
_RESERVE_ATTEMPTS = 3

logger = logging.getLogger(__name__)


def retention_for(view: Any) -> timedelta:
    """How long a stored response is replayed for this view.

    24 hours covers a person tapping "save" twice on a bad connection. A view
    whose creates are retried from a device outbox — where a phone can sit in a
    basement for days between an ambiguous send and its retry — declares
    `idempotency_retention` to keep its keys longer.
    """
    return getattr(view, "idempotency_retention", RETENTION)


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


def _owner(request: Request) -> tuple[Any, dict[str, Any]]:
    """The company and the principal a key is stored under.

    A staff user, or a customer portal account (M6) — which is not a User and
    has its own column, so the two can never read each other's stored answers.
    """
    principal = request.user
    if getattr(principal, "is_portal_account", False):
        return principal.company_id, {"user_id": None, "portal_account_id": principal.pk}  # type: ignore[union-attr]
    user = authenticated_user(request)
    return user.company_id, {"user_id": user.pk, "portal_account_id": None}


@dataclass(frozen=True)
class _Reservation:
    """The pending row this request holds, and when it took it.

    The timestamp is the fencing token: a reservation taken over after
    `PENDING_TIMEOUT` has a newer one, so a slow original that finally finishes
    can neither overwrite nor delete the row it no longer owns.
    """

    pk: int
    reserved_at: datetime

    def mine(self) -> QuerySet[IdempotencyKey]:
        return IdempotencyKey.objects.filter(
            pk=self.pk, created_at=self.reserved_at, response_status__isnull=True
        )


def _take_over(
    pk: int,
    candidate: QuerySet[IdempotencyKey],
    *,
    endpoint: str,
    fingerprint: str,
    now: datetime,
    retention: timedelta,
) -> _Reservation | None:
    """Turn an expired or abandoned row into this request's reservation.

    A conditional UPDATE, so of two requests taking over the same row exactly
    one matches: the second waits on the first's row lock and then finds the
    condition no longer holds.
    """
    updated = candidate.update(
        endpoint=endpoint,
        request_hash=fingerprint,
        response_status=None,
        response_body=None,
        created_at=now,
        expires_at=now + retention,
    )
    if updated != 1:
        return None
    return _Reservation(pk, now)


def _reserve(
    *,
    company_id: Any,
    owner: dict[str, Any],
    key: str,
    endpoint: str,
    fingerprint: str,
    retention: timedelta,
) -> _Reservation | Response:
    """Claim the key before the work, or say why this request may not run.

    Returns the reservation, or the stored response to replay. Raises 409 for a
    key reused with another body, or one whose first request is still running.
    """
    for attempt in range(_RESERVE_ATTEMPTS):
        now = timezone.now()
        try:
            # Its own transaction, committed before the handler starts, so an
            # overlapping request sees the row. The unique constraint decides
            # which of two simultaneous inserts wins. Durable, so that inside
            # someone else's transaction it fails loudly instead of becoming a
            # savepoint nobody else can see until that transaction commits.
            with transaction.atomic(durable=True):
                row = IdempotencyKey.objects.create(
                    company_id=company_id,
                    key=key,
                    **owner,
                    endpoint=endpoint,
                    request_hash=fingerprint,
                    response_status=None,
                    response_body=None,
                    expires_at=now + retention,
                )
            return _Reservation(row.pk, row.created_at)
        except IntegrityError as error:
            conflict = error

        existing = IdempotencyKey.objects.filter(company_id=company_id, key=key, **owner).first()
        if existing is None:
            # Once, this is the race where the first request failed and released
            # the key between our insert and our read: insert again. Twice, the
            # insert is failing on something other than the unique key, and that
            # error is the answer, not "in flight".
            if attempt == 0:
                continue
            raise conflict

        taken: _Reservation | None
        if existing.expires_at <= now:
            taken = _take_over(
                existing.pk,
                IdempotencyKey.objects.filter(pk=existing.pk, expires_at__lte=now),
                endpoint=endpoint,
                fingerprint=fingerprint,
                now=now,
                retention=retention,
            )
        elif existing.request_hash != fingerprint or existing.endpoint != endpoint:
            raise RecordInUse(ErrorCode.IDEMPOTENCY_KEY_REUSED)
        elif not existing.is_pending:
            return Response(existing.response_body, status=existing.response_status)
        elif existing.created_at > now - PENDING_TIMEOUT:
            # The first request is still working. Waiting for it would hold a
            # worker for as long as the slowest handler; the client is told to
            # come back, and then gets the stored answer.
            raise Conflict(ErrorCode.IDEMPOTENCY_KEY_IN_FLIGHT)
        else:
            taken = _take_over(
                existing.pk,
                IdempotencyKey.objects.filter(
                    pk=existing.pk,
                    response_status__isnull=True,
                    created_at__lt=now - PENDING_TIMEOUT,
                ),
                endpoint=endpoint,
                fingerprint=fingerprint,
                now=now,
                retention=retention,
            )
        if taken is not None:
            return taken
        # Someone else changed the row first; look again.

    raise Conflict(ErrorCode.IDEMPOTENCY_KEY_IN_FLIGHT)


def _release(reservation: _Reservation) -> None:
    """Drop the reservation of a request that did not succeed.

    Runs after the handler's transaction has rolled back, in its own. A failure
    here must not mask the handler's own error; the row is then abandoned and
    taken over after `PENDING_TIMEOUT` — safe, because the work it guarded was
    rolled back with it.
    """
    try:
        with transaction.atomic(durable=True):
            reservation.mine().delete()
    except Exception:
        logger.exception("Could not release idempotency key %s", reservation.pk)


class _NotSuccessful(Exception):
    """Carries a non-2xx answer out of the transaction, rolling its work back."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


class _ReservationLost(Exception):
    """The key was taken over while this request worked; its work must not stand."""


def _complete(reservation: _Reservation, response: Response, retention: timedelta) -> None:
    """Store the answer, inside the transaction that holds the work.

    Fenced on the reservation: zero rows means another request decided this one
    was abandoned and took the key over, so it is doing the work now and this
    request's copy is rolled back.

    A response with no `.data` (a plain HttpResponse or FileResponse) completes
    the key without a body: the work is guarded all the same, and a replay
    answers with the stored status and an empty body.
    """
    data = getattr(response, "data", None)
    updated = reservation.mine().update(
        response_status=response.status_code,
        response_body=None if data is None else json.loads(json.dumps(data, default=str)),
        expires_at=timezone.now() + retention,
    )
    if updated != 1:
        raise _ReservationLost


def replay_protected(handler: Callable[..., Response]) -> Callable[..., Response]:
    """Wrap a create view so the same key returns the same answer.

    The rule that matters: the same key with a *different* body is refused with
    409 rather than served the stored response. Returning the old answer there
    would reply to a question that was never asked, and that is one of the
    hardest classes of bug to trace afterwards.

    Never done twice, never half done: the handler runs inside one transaction
    with the write that completes its key, so the work and its answer commit
    together or not at all. The handler's own atomic blocks become savepoints
    of it, and its on-commit hooks (the mails) fire only once the key is
    complete. The reservation commits before, and a release after a rollback,
    each in a transaction of its own.
    """

    @functools.wraps(handler)
    def wrapper(self: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
        key = request.headers.get(HEADER, "").strip()
        if not key:
            # A viewset whose creates are retried from a device outbox says so,
            # and a client that forgot the header is told rather than served:
            # without it, the retry the header exists for records twice.
            if getattr(self, "idempotency_key_required", False):
                raise BadRequest(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
            return handler(self, request, *args, **kwargs)

        endpoint = f"{request.method} {request.path}"
        fingerprint = _fingerprint(request)
        company_id, owner = _owner(request)
        if company_id is None:
            # The stored row is keyed by company, and a user without one cannot
            # reach any of the endpoints this wraps — every one of them is
            # company-scoped and the permission class refuses first.
            return handler(self, request, *args, **kwargs)

        retention = retention_for(self)
        reserved = _reserve(
            company_id=company_id,
            owner=owner,
            key=key,
            endpoint=endpoint,
            fingerprint=fingerprint,
            retention=retention,
        )
        if isinstance(reserved, Response):
            return reserved

        try:
            with transaction.atomic():
                response = handler(self, request, *args, **kwargs)
                # Only successful creates are worth replaying. Storing a failure
                # would make a transient error permanent for the next
                # twenty-four hours, and keeping the reservation would refuse
                # the retry that fixes it.
                if not 200 <= response.status_code < 300:
                    raise _NotSuccessful(response)
                _complete(reserved, response, retention)
        except _NotSuccessful as outcome:
            _release(reserved)
            return outcome.response
        except _ReservationLost:
            # Not released: the key is the new owner's now.
            raise Conflict(ErrorCode.IDEMPOTENCY_KEY_IN_FLIGHT) from None
        except BaseException:
            _release(reserved)
            raise
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

    #: Contributed by the viewset this is mixed into, and rebound below. Declared
    #: so that the rebinding is a typed assignment rather than an attribute this
    #: class is not supposed to know about.
    create: Callable[..., Response]

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
