"""Check-digit algorithms and Turkish text folding.

Both are the kind of code that looks right and is wrong, and both fail quietly:
a broken check digit lets a typo onto a signed contract, and broken folding
makes half the address table unreachable from search.
"""

from __future__ import annotations

import pytest
from rest_framework import serializers

from core.text import normalize
from core.validators import normalize_phone, validate_national_id, validate_tax_number


class TestNationalId:
    def test_accepts_a_valid_number(self):
        assert validate_national_id("10000000146") == "10000000146"

    @pytest.mark.parametrize(
        "value",
        [
            "10000000147",  # last check digit wrong
            "10000000156",  # tenth digit wrong
            "01234567890",  # cannot start with zero
            "1234567890",  # ten digits
            "123456789012",  # twelve digits
            "1000000014a",  # not all digits
            "",
        ],
    )
    def test_rejects_invalid(self, value):
        with pytest.raises(serializers.ValidationError):
            validate_national_id(value)


class TestTaxNumber:
    def test_accepts_a_valid_number(self):
        assert validate_tax_number("4540536920") == "4540536920"

    def test_eleven_digits_is_treated_as_a_national_id(self):
        # A sole trader bills under their personal number, so the value is
        # routed to the other algorithm rather than rejected outright.
        assert validate_tax_number("10000000146") == "10000000146"

    @pytest.mark.parametrize("value", ["4540536921", "123456789", "abcdefghij", ""])
    def test_rejects_invalid(self, value):
        with pytest.raises(serializers.ValidationError):
            validate_tax_number(value)


class TestPhone:
    @pytest.mark.parametrize(
        "value",
        ["0555 123 45 67", "+90 555 123 45 67", "5551234567", "+905551234567", "0 555 1234567"],
    )
    def test_every_way_of_typing_it_folds_to_one_value(self, value):
        # Stored as typed, the same person would look like five contacts.
        assert normalize_phone(value) == "+905551234567"

    @pytest.mark.parametrize("value", ["555123456", "55512345678", "", "not a phone"])
    def test_rejects_invalid(self, value):
        with pytest.raises(serializers.ValidationError):
            normalize_phone(value)


class TestTurkishNormalisation:
    def test_the_dotted_capital_i(self):
        # str.lower() would produce "i" plus a combining dot above — two code
        # points — which never matches the plain "i" in the database, and fails
        # without raising anything.
        assert normalize("İSTANBUL") == "istanbul"
        assert "̇" not in normalize("İSTANBUL")

    def test_the_dotless_i(self):
        assert normalize("Işıklar") == "isiklar"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Şişli", "sisli"),
            ("Göztepe", "goztepe"),
            ("Üsküdar", "uskudar"),
            ("Çankaya", "cankaya"),
            ("Ğ", "g"),
            ("  Kadıköy  ", "kadikoy"),
        ],
    )
    def test_folds_every_turkish_letter(self, raw, expected):
        assert normalize(raw) == expected

    def test_a_user_typed_query_finds_the_stored_form(self):
        # The whole point: the loader and the search must agree character for
        # character, so both call this one function.
        assert normalize("sisli") == normalize("Şişli")
        assert normalize("istanbul") == normalize("İstanbul")
