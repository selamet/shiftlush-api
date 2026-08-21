"""National IDs must not be readable from the database."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from apps.companies.models import Company
from apps.customers.models import Customer, CustomerType
from core.context import company_context, system_context
from core.crypto import DecryptionError, decrypt, encrypt, generate_key

TCKN = "10000000146"


@pytest.fixture
def company(db) -> Company:
    with system_context():
        return Company.objects.create(legal_name="Test Ltd", display_name="Test")


class TestCipher:
    def test_round_trip(self):
        assert decrypt(encrypt(TCKN)) == TCKN

    def test_same_value_encrypts_differently_each_time(self):
        # A fresh nonce per write is what stops anyone with read access from
        # confirming whether a given person is in the database.
        assert encrypt(TCKN) != encrypt(TCKN)

    def test_empty_stays_empty(self):
        # Encrypting "" would turn "not provided" into something that looks
        # like data.
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_tampering_is_detected(self):
        stored = encrypt(TCKN)
        tampered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
        with pytest.raises((DecryptionError, ValueError)):
            decrypt(tampered)

    def test_plaintext_from_before_encryption_still_reads(self):
        # Rows written before the column was encrypted must keep working while
        # a migration re-encrypts them, rather than failing every read.
        assert decrypt("10000000146") == "10000000146"

    def test_missing_key_is_a_configuration_error(self, settings):
        settings.FIELD_ENCRYPTION_KEY = ""
        with pytest.raises(ImproperlyConfigured):
            encrypt(TCKN)

    def test_wrong_key_cannot_decrypt(self, settings):
        stored = encrypt(TCKN)
        settings.FIELD_ENCRYPTION_KEY = generate_key()
        with pytest.raises(DecryptionError):
            decrypt(stored)


class TestModelField:
    def test_value_reads_back_as_plaintext(self, company):
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company,
                type=CustomerType.INDIVIDUAL,
                legal_name="Individual",
                national_id=TCKN,
            )
            assert Customer.objects.get(pk=customer.pk).national_id == TCKN

    def test_database_never_holds_the_plaintext(self, company):
        with company_context(company.id):
            Customer.objects.create(
                company=company,
                type=CustomerType.INDIVIDUAL,
                legal_name="Individual",
                national_id=TCKN,
            )

        # The point of the whole exercise: anyone reading the table directly —
        # a backup, a support query, a leaked dump — sees ciphertext. Read
        # without a WHERE clause so the assertion does not depend on how each
        # engine happens to store a UUID.
        with connection.cursor() as cursor:
            cursor.execute("SELECT national_id FROM customer")
            stored = cursor.fetchone()[0]

        assert stored != TCKN
        assert TCKN not in stored
        assert stored.startswith("v1:")

    def test_blank_is_stored_blank(self, company):
        with company_context(company.id):
            Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Company"
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT national_id FROM customer")
            assert cursor.fetchone()[0] == ""
