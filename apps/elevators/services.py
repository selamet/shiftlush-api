"""Elevator rules that are not a single field."""

from __future__ import annotations

import secrets

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
    """Set a fresh token, retrying on the vanishingly rare collision.

    Twelve characters of this alphabet is about 70 bits, so a clash is not
    something that happens — but it is something the database can refuse, and
    swallowing that would leave two elevators sharing a sticker. Three attempts,
    then the error is allowed to surface.
    """
    for _ in range(MAX_ATTEMPTS):
        candidate = generate_qr_token()
        if not Elevator.unscoped.filter(qr_token=candidate).exists():
            elevator.qr_token = candidate
            return candidate
    raise IntegrityError("Could not allocate a unique QR token after several attempts.")


@transaction.atomic
def regenerate_qr_token(elevator: Elevator) -> str:
    """Issue a new token and invalidate the old one.

    Used when a label has been copied. Every sticker already printed for this
    elevator stops resolving, which is the point — and why the interface has to
    say so before the user confirms.
    """
    from django.utils import timezone

    token = assign_qr_token(elevator)
    elevator.qr_token_generated_at = timezone.now()
    elevator.save(update_fields=["qr_token", "qr_token_generated_at", "updated_at"])
    return token
