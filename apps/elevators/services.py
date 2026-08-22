"""Elevator rules that are not a single field."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from django.db import IntegrityError, transaction

from apps.elevators.models import Elevator

# URL-safe, unambiguous alphabet. No 0/O or 1/l/I: these end up on a printed
# sticker that someone will read aloud over the phone from a machine room.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TOKEN_LENGTH = 12
MAX_ATTEMPTS = 3


def generate_qr_token() -> str:
    """A random token, never derived from the record.

    Deriving it from the id or the registration number would make it guessable,
    and a competitor could walk a company's whole estate by incrementing.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LENGTH))


def assign_qr_token(elevator: Elevator) -> str:
    """Put a fresh token on the instance without saving it.

    No uniqueness query here, deliberately. Asking the database whether a token
    is free settles nothing: the answer is stale the moment it is given, and the
    column's unique index is what actually decides. Callers that go on to write
    the row use `save_with_qr_token`, which retries the answer that counts.
    """
    elevator.qr_token = generate_qr_token()
    return elevator.qr_token


def _is_token_collision(error: BaseException) -> bool:
    """Whether this integrity error is about the token rather than something else.

    A duplicate registration number arrives here as an `IntegrityError` too, and
    retrying that would spend three round trips before surfacing the same error
    the caller could have had at once. Both engines name the column: PostgreSQL
    through the constraint name, SQLite through the column itself.
    """
    return "qr_token" in str(error)


def save_with_qr_token[T](save: Callable[[str], T]) -> T:
    """Write a record with a fresh token, regenerating if the database refuses it.

    Twelve characters of this alphabet is about seventy bits, so a clash is not
    something that happens — but it is something the database can refuse, and
    the refusal lands on the insert, not on any check made before it. Between
    reading "this token is free" and writing it, another request can take it;
    the loser of that race used to receive a constraint violation from a plain
    create, over a value it never chose and cannot correct by editing its own
    request. Section 11.1 asks for that error to be caught and the token
    regenerated, at most three times, and never for a duplicate to be kept
    quietly.

    Each attempt gets its own savepoint: a failed statement leaves the enclosing
    transaction unusable in PostgreSQL, so without one the retry would fail on
    whatever it ran next rather than on the token.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with transaction.atomic():
                return save(generate_qr_token())
        except IntegrityError as error:
            if attempt == MAX_ATTEMPTS or not _is_token_collision(error):
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def regenerate_qr_token(elevator: Elevator) -> str:
    """Issue a new token and invalidate the old one.

    Used when a label has been copied. Every sticker already printed for this
    elevator stops resolving, which is the point — and why the interface has to
    say so before the user confirms.
    """
    from django.utils import timezone

    def write(token: str) -> str:
        elevator.qr_token = token
        elevator.qr_token_generated_at = timezone.now()
        elevator.save(update_fields=["qr_token", "qr_token_generated_at", "updated_at"])
        return token

    return save_with_qr_token(write)
