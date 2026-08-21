"""Algorithmic validation, kept in one module.

Serializers are the single source of truth for validation rules; these are the
few checks that involve an algorithm rather than a field definition, so they
live here instead of being retyped wherever they are needed.

Everything raises DRF's ValidationError with a machine-readable code. The
backend never returns Turkish: the code is what travels, and the frontend maps
it to a message.
"""

from __future__ import annotations

import re

from rest_framework import serializers

from core.error_codes import ErrorCode


def validate_national_id(value: str) -> str:
    """Turkish national identification number (TCKN).

    Eleven digits with two check digits. Rejecting a malformed one at entry is
    worth the twenty lines: it is printed on signed contracts, and a typo found
    later means reissuing the document.
    """
    if not re.fullmatch(r"\d{11}", value):
        raise serializers.ValidationError(code=ErrorCode.INVALID_NATIONAL_ID)
    digits = [int(char) for char in value]
    if digits[0] == 0:
        raise serializers.ValidationError(code=ErrorCode.INVALID_NATIONAL_ID)

    odd_sum = sum(digits[0:9:2])
    even_sum = sum(digits[1:8:2])
    if (odd_sum * 7 - even_sum) % 10 != digits[9]:
        raise serializers.ValidationError(code=ErrorCode.INVALID_NATIONAL_ID)
    if sum(digits[:10]) % 10 != digits[10]:
        raise serializers.ValidationError(code=ErrorCode.INVALID_NATIONAL_ID)
    return value


def validate_tax_number(value: str) -> str:
    """Turkish tax number (VKN), ten digits with a check digit.

    An eleven digit value is a TCKN — a sole trader billing under their personal
    number — so it is routed to that check rather than rejected.
    """
    if len(value) == 11:
        return validate_national_id(value)
    if not re.fullmatch(r"\d{10}", value):
        raise serializers.ValidationError(code=ErrorCode.INVALID_TAX_NUMBER)

    digits = [int(char) for char in value]
    total = 0
    for index in range(9):
        temp = (digits[index] + 10 - (index + 1)) % 10
        if temp == 9:
            total += temp
        else:
            total += (temp * pow(2, 9 - index)) % 9
    if (10 - total % 10) % 10 != digits[9]:
        raise serializers.ValidationError(code=ErrorCode.INVALID_TAX_NUMBER)
    return value


def normalize_phone(value: str) -> str:
    """Fold the ways a Turkish mobile number gets typed into E.164.

    `0555 123 45 67`, `+90 555 123 45 67` and `5551234567` are the same number;
    storing them as typed would make the same person look like three contacts.
    """
    digits = re.sub(r"\D", "", value)
    if digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10:
        raise serializers.ValidationError(code=ErrorCode.INVALID_PHONE)
    return f"+90{digits}"


def normalize_email(value: str) -> str:
    return value.strip().lower()
