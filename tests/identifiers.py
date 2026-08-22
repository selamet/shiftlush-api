"""Valid Turkish identifiers for tests.

Every customer now needs one, and a hand-typed number that fails its check digit
turns an unrelated test into a puzzle about validation. Generated from a seed so
each row in a loop gets its own, which the uniqueness constraint requires.
"""

from __future__ import annotations


def tax_number(seed: int = 0) -> str:
    """A ten-digit VKN whose check digit is correct."""
    body = f"{seed:09d}"
    digits = [int(char) for char in body]
    total = 0
    for index in range(9):
        shifted = (digits[index] + 10 - (index + 1)) % 10
        total += shifted if shifted == 9 else (shifted * pow(2, 9 - index)) % 9
    return body + str((10 - total % 10) % 10)


def national_id(seed: int = 0) -> str:
    """An eleven-digit TCKN whose two check digits are correct."""
    body = f"{seed % 900_000_000 + 100_000_000:09d}"
    digits = [int(char) for char in body]
    tenth = ((sum(digits[0:9:2]) * 7) - sum(digits[1:8:2])) % 10
    eleventh = (sum(digits) + tenth) % 10
    return f"{body}{tenth}{eleventh}"
