"""F0: the ledger. Accounts, cash accounts and the movements between them.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. A balance is never
stored: it is the sum of the movements, so the tests here create movements
directly and ask the aggregate queries what they add up to.
"""

from __future__ import annotations

import csv
import importlib
import io
import random
import threading
import unittest
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.apps import apps as global_apps
from django.core import mail as django_mail
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import QuerySet
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.billing import services as billing
from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.companies.models import Company
from apps.contracts.models import BillingPeriod, Contract, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerContact, CustomerType
from apps.ledger import services as ledger_services
from apps.ledger.balances import account_balance, cash_balance, with_balance, with_cash_balance
from apps.ledger.migration_helpers import carry_over_invoices
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
    CashAccountRole,
    CashDirection,
    CashKind,
    Collection,
    CollectionApplication,
    Direction,
    DocumentCounter,
    Movement,
    MovementImmutableError,
    MovementSource,
    OpeningBalance,
    PaymentMethod,
    Supplier,
    Transfer,
    TransferKind,
)
from apps.ledger.posting import check_date, post, reverse_document
from apps.ledger.services import (
    DEFAULT_CASH_ACCOUNT_NAME,
    account_for_customer,
    cancel_collection,
    cancel_manual_entry,
    cancel_opening,
    cancel_payment_out,
    cancel_transfer,
    invoice_collected,
    invoice_remaining,
    next_number,
    record_collection,
    record_manual_entry,
    record_opening,
    record_payment_out,
    record_transfer,
)
from apps.ledger.statement import (
    build_statement_html,
    render_statement_pdf,
    send_statement,
    statement_csv,
    statement_rows,
)
from apps.users.models import Role, User, UserCustomer
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context
from core.exceptions import Conflict
from core.scheduling import local_today
from tests.identifiers import tax_number

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


def make_firm(letter: str) -> tuple[Company, User]:
    return register_company(
        legal_name=f"Firm {letter} Ltd",
        display_name=f"Firm {letter}",
        first_name=letter,
        last_name="Owner",
        email=f"{letter.lower()}@example.com",
        password=PASSWORD,
    )


def make_customer(company: Company, name: str = "Customer") -> Customer:
    with company_context(company.id):
        return Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name=name
        )


def move(**fields: Any) -> Movement:
    """An ORM fixture: writes a movement directly, with no rules applied."""
    fields.setdefault("date", date(2026, 10, 1))
    fields.setdefault("currency", "TRY")
    fields.setdefault("source", MovementSource.MANUAL)
    return Movement.objects.create(**fields)


@pytest.fixture
def firm(db) -> tuple[Company, User]:
    return make_firm("A")


class TestCustomerAccounts:
    def test_a_new_customer_has_an_account(self, firm):
        company, owner = firm
        response = api_for(owner).post(
            reverse("customer-list"),
            {
                "type": CustomerType.CORPORATE,
                "legal_name": "Kule Yonetim",
                "tax_number": tax_number(1),
            },
        )
        assert response.status_code == 201, response.data

        with company_context(company.id):
            accounts = list(Account.objects.filter(customer_id=response.data["id"]))
        assert len(accounts) == 1
        assert accounts[0].kind == AccountKind.CUSTOMER
        assert accounts[0].name == "Kule Yonetim"
        assert accounts[0].currency == "TRY"
        assert accounts[0].is_active

    def test_an_account_follows_its_customers_name(self, firm):
        company, owner = firm
        customer = make_customer(company, "Old name")

        response = api_for(owner).patch(
            reverse("customer-detail", args=[customer.pk]), {"legal_name": "New name"}
        )
        assert response.status_code == 200, response.data

        with company_context(company.id):
            account = Account.objects.get(customer=customer)
        assert account.name == "New name"

    def test_saving_a_customer_again_makes_no_second_account(self, firm):
        company, _ = firm
        customer = make_customer(company)
        with company_context(company.id):
            customer.save()
            assert account_for_customer(customer) == Account.objects.get(customer=customer)
            assert Account.objects.filter(customer=customer).count() == 1

    def test_an_account_names_exactly_one_party(self, firm):
        company, owner = firm
        customer = make_customer(company)
        with company_context(company.id):
            supplier = Supplier.objects.create(company=company, name="Parca AS")

            # Zero parties.
            with pytest.raises(IntegrityError), transaction.atomic():
                Account.objects.create(company=company, kind=AccountKind.SUPPLIER, name="Nobody")

            # Two parties.
            with pytest.raises(IntegrityError), transaction.atomic():
                Account.objects.create(
                    company=company,
                    kind=AccountKind.SUPPLIER,
                    supplier=supplier,
                    user=owner,
                    name="Both",
                )

            # One party, but not the one its kind names.
            with pytest.raises(IntegrityError), transaction.atomic():
                Account.objects.create(
                    company=company, kind=AccountKind.STAFF, supplier=supplier, name="Wrong"
                )

            # And one per party.
            with pytest.raises(IntegrityError), transaction.atomic():
                Account.objects.create(
                    company=company, kind=AccountKind.CUSTOMER, customer=customer, name="Again"
                )

            Account.objects.create(
                company=company, kind=AccountKind.SUPPLIER, supplier=supplier, name="Parca AS"
            )
            Account.objects.create(
                company=company, kind=AccountKind.STAFF, user=owner, name="A Owner"
            )


class TestCashAccounts:
    def test_a_new_company_has_merkez_kasa(self, firm):
        company, _ = firm
        with company_context(company.id):
            tills = list(CashAccount.objects.all())
        assert [(till.kind, till.name, till.currency) for till in tills] == [
            (CashKind.CASH, DEFAULT_CASH_ACCOUNT_NAME, "TRY")
        ]
        assert DEFAULT_CASH_ACCOUNT_NAME == "Merkez kasa"


class TestDataMigration:
    def test_the_migration_gives_existing_customers_accounts_once(self, firm):
        company, _ = firm
        other, _ = make_firm("B")
        first = make_customer(company, "First")
        second = make_customer(other, "Second")
        # As if they predated the ledger: no account, and no till either.
        with system_context():
            Account.unscoped.get_queryset().hard_delete()
            CashAccount.unscoped.get_queryset().hard_delete()

        migration = importlib.import_module("apps.ledger.migrations.0002_accounts_and_merkez_kasa")
        # With the live models, whose managers are tenant-scoped, the function
        # needs the system context a migration's historical models do without.
        with system_context():
            migration.open_the_books(global_apps, None)
            migration.open_the_books(global_apps, None)

        with system_context():
            accounts = sorted(
                Account.unscoped.values_list("company_id", "customer_id", "name", "kind")
            )
            tills = sorted(CashAccount.unscoped.values_list("company_id", "name", "kind"))
        assert accounts == sorted(
            [
                (company.id, first.id, "First", AccountKind.CUSTOMER),
                (other.id, second.id, "Second", AccountKind.CUSTOMER),
            ]
        )
        assert tills == sorted(
            [
                (company.id, "Merkez kasa", CashKind.CASH),
                (other.id, "Merkez kasa", CashKind.CASH),
            ]
        )


class TestBalances:
    def test_balances_are_sums(self, firm):
        company, _ = firm
        customer = make_customer(company)
        with company_context(company.id):
            account = Account.objects.get(customer=customer)
            idle = Account.objects.create(
                company=company,
                kind=AccountKind.SUPPLIER,
                supplier=Supplier.objects.create(company=company, name="Idle"),
                name="Idle",
            )
            till = CashAccount.objects.get()
            bank = CashAccount.objects.create(company=company, kind=CashKind.BANK, name="Bank")

            move(company=company, account=account, direction="debit", amount=Decimal("1000.00"))
            move(
                company=company,
                account=account,
                direction="credit",
                amount=Decimal("250.50"),
                cash_account=till,
                cash_direction="in",
                source=MovementSource.COLLECTION,
            )
            move(
                company=company,
                cash_account=till,
                cash_direction="out",
                amount=Decimal("100.00"),
                source=MovementSource.TRANSFER,
            )
            move(
                company=company,
                cash_account=bank,
                cash_direction="in",
                amount=Decimal("100.00"),
                source=MovementSource.TRANSFER,
            )

            assert account_balance(account) == Decimal("749.50")
            assert account_balance(idle) == Decimal("0")
            # An annotation lands in the instance's __dict__; read it there so
            # the type checker, which cannot see it on the model, has no say.
            balances = {row.pk: vars(row)["balance"] for row in with_balance(Account.objects.all())}
            assert balances == {account.pk: Decimal("749.50"), idle.pk: Decimal("0")}

            assert cash_balance(till) == Decimal("150.50")
            assert cash_balance(bank) == Decimal("100.00")
            cash = {
                row.pk: vars(row)["balance"] for row in with_cash_balance(CashAccount.objects.all())
            }
            assert cash == {till.pk: Decimal("150.50"), bank.pk: Decimal("100.00")}


class TestMovementConstraints:
    def test_an_invoice_posts_once(self, firm):
        company, _ = firm
        customer = make_customer(company)
        document_id = customer.pk  # any UUID will do for the constraint
        with company_context(company.id):
            account = Account.objects.get(customer=customer)
            fields = {
                "company": company,
                "account": account,
                "direction": "debit",
                "amount": Decimal("10.00"),
                "source": MovementSource.INVOICE,
                "document_type": "billing.invoicedraft",
                "document_id": document_id,
            }
            move(**fields)
            with pytest.raises(IntegrityError), transaction.atomic():
                move(**fields)
            # A manual entry is not keyed on its document this way.
            move(**{**fields, "source": MovementSource.MANUAL})
            move(**{**fields, "source": MovementSource.MANUAL})

    def test_a_movement_is_reversed_once(self, firm):
        company, _ = firm
        customer = make_customer(company)
        with company_context(company.id):
            account = Account.objects.get(customer=customer)
            original = move(
                company=company, account=account, direction="debit", amount=Decimal("5.00")
            )
            move(
                company=company,
                account=account,
                direction="credit",
                amount=Decimal("5.00"),
                source=MovementSource.REVERSAL,
                reverses=original,
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                move(
                    company=company,
                    account=account,
                    direction="credit",
                    amount=Decimal("5.00"),
                    source=MovementSource.REVERSAL,
                    reverses=original,
                )

    def test_amounts_are_positive(self, firm):
        company, _ = firm
        customer = make_customer(company)
        with company_context(company.id):
            account = Account.objects.get(customer=customer)
            with pytest.raises(IntegrityError), transaction.atomic():
                move(company=company, account=account, direction="debit", amount=Decimal("0"))

    def test_every_source_the_spec_names_exists(self):
        assert set(MovementSource.values) == {
            "invoice",
            "invoice_cancel",
            "collection",
            "payment_out",
            "manual",
            "transfer",
            "opening",
            "reversal",
        }


class TestIsolation:
    def test_another_firms_ledger_is_invisible(self, firm):
        company, _ = firm
        other, _ = make_firm("B")
        make_customer(other, "Theirs")
        with company_context(company.id):
            assert not Account.objects.filter(name="Theirs").exists()
            assert CashAccount.objects.count() == 1
        with company_context(other.id):
            assert Account.objects.filter(name="Theirs").count() == 1


def codes(error: Any) -> Any:
    return error.value.get_codes()


@pytest.fixture
def books(firm):
    """A customer account and the firm's till, ready for posting."""
    company, owner = firm
    customer = make_customer(company)
    with company_context(company.id):
        yield SimpleNamespace(
            company=company,
            owner=owner,
            customer=customer,
            account=Account.objects.get(customer=customer),
            till=CashAccount.objects.get(),
        )


def collect(books: SimpleNamespace, amount: str = "100.00", **overrides: Any) -> Movement:
    fields: dict[str, Any] = {
        "account": books.account,
        "cash_account": books.till,
        "direction": Direction.CREDIT,
        "cash_direction": CashDirection.IN,
        "amount": Decimal(amount),
        "currency": "TRY",
        "date": local_today(),
        "source": MovementSource.COLLECTION,
        "document": books.customer,
        "description": "Collected",
        "by": books.owner,
    }
    fields.update(overrides)
    return post(**fields)


class TestPosting:
    def test_post_writes_one_movement_with_both_sides(self, books):
        movement = collect(books, "250.50")

        assert Movement.objects.count() == 1
        stored = Movement.objects.get()
        assert stored == movement
        assert stored.company_id == books.company.id
        assert (stored.account, stored.direction) == (books.account, "credit")
        assert (stored.cash_account, stored.cash_direction) == (books.till, "in")
        assert stored.amount == Decimal("250.50")
        assert stored.currency == "TRY"
        assert stored.date == local_today()
        assert stored.source == "collection"
        assert stored.document_type == "customers.customer"
        assert stored.document_id == books.customer.pk
        assert stored.description == "Collected"
        assert stored.created_by == books.owner
        assert stored.reverses is None
        assert account_balance(books.account) == Decimal("-250.50")
        assert cash_balance(books.till) == Decimal("250.50")

    def test_a_currency_mismatch_writes_nothing(self, books):
        dollars = CashAccount.objects.create(
            company=books.company, kind=CashKind.BANK, name="USD", currency="USD"
        )
        for currency in ("TRY", "USD"):
            with pytest.raises(ValidationError) as error:
                collect(books, cash_account=dollars, currency=currency)
            assert codes(error) == {"currency": "CURRENCY_MISMATCH"}
        assert Movement.objects.count() == 0

    def test_amount_must_be_positive(self, books):
        for amount in ("0", "-5.00"):
            with pytest.raises(ValidationError) as error:
                collect(books, amount)
            assert codes(error) == {"amount": "invalid"}
        assert Movement.objects.count() == 0

    def test_dates_after_today_or_in_a_closed_period_are_refused(self, books):
        today = local_today()
        with pytest.raises(ValidationError) as error:
            collect(books, date=today + timedelta(days=1))
        assert codes(error) == {"date": "invalid"}

        books.company.ledger_closed_through = today - timedelta(days=10)
        books.company.save()
        for day in (today - timedelta(days=10), today - timedelta(days=40)):
            with pytest.raises(ValidationError) as error:
                collect(books, date=day)
            assert codes(error) == {"date": "PERIOD_CLOSED"}
            with pytest.raises(ValidationError):
                check_date(books.company, day)
        assert Movement.objects.count() == 0

        collect(books, date=today - timedelta(days=9))
        check_date(books.company, today)
        assert Movement.objects.count() == 1

    def test_reverse_mirrors_and_only_once(self, books):
        invoice = collect(
            books,
            "1000.00",
            cash_account=None,
            cash_direction=None,
            direction=Direction.DEBIT,
            source=MovementSource.INVOICE,
        )
        collection = collect(books, "400.00")

        reversals = reverse_document(books.customer, date=local_today(), by=books.owner)

        assert len(reversals) == 2
        by_original = {r.reverses_id: r for r in reversals}
        assert set(by_original) == {invoice.pk, collection.pk}
        for original in (invoice, collection):
            mirror = by_original[original.pk]
            assert mirror.source == "reversal"
            assert mirror.amount == original.amount
            assert mirror.currency == original.currency
            assert mirror.account == original.account
            assert mirror.cash_account == original.cash_account
            assert (mirror.document_type, mirror.document_id) == (
                original.document_type,
                original.document_id,
            )
            assert mirror.created_by == books.owner
        assert by_original[invoice.pk].direction == "credit"
        assert by_original[invoice.pk].cash_direction == ""
        assert by_original[collection.pk].direction == "debit"
        assert by_original[collection.pk].cash_direction == "out"
        assert account_balance(books.account) == Decimal("0")
        assert cash_balance(books.till) == Decimal("0")

        with pytest.raises(Conflict) as error:
            reverse_document(books.customer, date=local_today(), by=books.owner)
        assert codes(error) == "ALREADY_REVERSED"
        assert Movement.objects.count() == 4

    def test_reversing_a_document_with_no_movements_is_refused(self, books):
        with pytest.raises(Conflict) as error:
            reverse_document(books.customer, date=local_today(), by=books.owner)
        assert codes(error) == "ALREADY_REVERSED"

    def test_invariant_after_a_random_sequence(self, books):
        rng = random.Random(5)  # noqa: S311
        company = books.company
        accounts = [books.account] + [
            Account.objects.create(
                company=company,
                kind=AccountKind.SUPPLIER,
                supplier=Supplier.objects.create(company=company, name=f"Supplier {n}"),
                name=f"Supplier {n}",
            )
            for n in range(2)
        ]
        tills = [
            books.till,
            CashAccount.objects.create(company=company, kind=CashKind.BANK, name="Bank"),
        ]
        documents = [
            Supplier.objects.create(company=company, name=f"Document {n}") for n in range(8)
        ]
        expected: dict[Any, Decimal] = {side.pk: Decimal("0") for side in accounts + tills}
        # Per document, the signed effects of its movements not yet reversed.
        open_effects: dict[Any, list[list[tuple[Any, Decimal]]]] = {doc.pk: [] for doc in documents}

        for _ in range(200):
            document = rng.choice(documents)
            if rng.random() < 0.25:
                pending = open_effects[document.pk]
                if pending:
                    reverse_document(document, date=local_today(), by=books.owner)
                    for effect in pending:
                        for side, signed in effect:
                            expected[side] -= signed
                    open_effects[document.pk] = []
                else:
                    with pytest.raises(Conflict):
                        reverse_document(document, date=local_today(), by=books.owner)
                continue

            shape = rng.choice(["account", "cash", "both"])
            account = rng.choice(accounts) if shape != "cash" else None
            till = rng.choice(tills) if shape != "account" else None
            direction = rng.choice(Direction.values) if account else None
            cash_direction = rng.choice(CashDirection.values) if till else None
            amount = Decimal(rng.randint(1, 100_000)) / 100
            post(
                account=account,
                cash_account=till,
                direction=direction,
                cash_direction=cash_direction,
                amount=amount,
                currency="TRY",
                date=local_today(),
                source=MovementSource.MANUAL,
                document=document,
                description="",
                by=books.owner,
            )
            effect = []
            if account is not None:
                effect.append((account.pk, amount if direction == Direction.DEBIT else -amount))
            if till is not None:
                effect.append((till.pk, amount if cash_direction == CashDirection.IN else -amount))
            for side, signed in effect:
                expected[side] += signed
            open_effects[document.pk].append(effect)

        for account in accounts:
            assert account_balance(account) == expected[account.pk]
        for till in tills:
            assert cash_balance(till) == expected[till.pk]
        listed = {row.pk: vars(row)["balance"] for row in with_balance(Account.objects.all())}
        assert listed == {account.pk: expected[account.pk] for account in accounts}

    def test_movements_cannot_be_edited(self, books):
        movement = collect(books)

        movement.description = "Edited"
        with pytest.raises(MovementImmutableError):
            movement.save()
        with pytest.raises(MovementImmutableError):
            movement.delete()
        with pytest.raises(MovementImmutableError):
            movement.hard_delete()

        stored = Movement.objects.get()
        assert stored.description == "Collected"
        assert not stored.is_deleted


# Documents ----------------------------------------------------------------------


def make_invoice(
    books: SimpleNamespace,
    total: str = "1000.00",
    *,
    status: str = InvoiceStatus.ISSUED,
    currency: str = "TRY",
    customer: Customer | None = None,
) -> InvoiceDraft:
    """An issued invoice draft for the books' customer, with a frozen total."""
    customer = customer or books.customer
    books.invoices = getattr(books, "invoices", 0) + 1
    n = books.invoices
    with company_context(customer.company_id):
        contract = Contract.objects.create(
            company_id=customer.company_id,
            customer=customer,
            contract_number=f"C-{n}",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
            billing_period=BillingPeriod.MONTHLY,
        )
        issued = status != InvoiceStatus.DRAFT
        return InvoiceDraft.objects.create(
            company_id=customer.company_id,
            contract=contract,
            customer=customer,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            status=status,
            currency=currency,
            vat_rate="20.00",
            total=Decimal(total),
            invoice_number=f"F-{n}" if issued else "",
            invoice_date=date(2026, 10, 1) if issued else None,
            paid_on=date(2026, 10, 1) if status == InvoiceStatus.PAID else None,
        )


def receive(
    books: SimpleNamespace,
    amount: str = "100.00",
    applies: list[tuple[InvoiceDraft, Decimal]] | None = None,
    **overrides: Any,
) -> Collection:
    fields: dict[str, Any] = {
        "account": books.account,
        "cash_account": books.till,
        "amount": Decimal(amount),
        "method": PaymentMethod.CASH,
        "date": local_today(),
        "note": "",
        "applies": applies or [],
        "by": books.owner,
    }
    fields.update(overrides)
    return record_collection(**fields)


def supplier_account(books: SimpleNamespace, name: str = "Parts Co") -> Account:
    supplier = Supplier.objects.create(company=books.company, name=name)
    return Account.objects.create(
        company=books.company, kind=AccountKind.SUPPLIER, supplier=supplier, name=name
    )


def movements_of(document: Any) -> list[Movement]:
    return list(
        Movement.objects.filter(
            document_type=f"{document._meta.app_label}.{document._meta.model_name}",
            document_id=document.pk,
        ).order_by("created_at", "id")
    )


class TestDocuments:
    def test_a_collection_credits_the_account_and_fills_the_till(self, books):
        collection = receive(books, "250.00", note="Ekim")

        assert collection.number == "T-000001"
        assert collection.company_id == books.company.id
        assert collection.method == "cash"
        assert collection.created_by == books.owner
        assert collection.cancelled_at is None
        [movement] = movements_of(collection)
        assert movement.source == "collection"
        assert (movement.account, movement.direction) == (books.account, "credit")
        assert (movement.cash_account, movement.cash_direction) == (books.till, "in")
        assert movement.amount == Decimal("250.00")
        assert account_balance(books.account) == Decimal("-250.00")
        assert cash_balance(books.till) == Decimal("250.00")

    def test_numbers_are_sequential_per_firm(self, books):
        assert receive(books).number == "T-000001"
        assert receive(books).number == "T-000002"
        assert (
            record_payment_out(
                account=supplier_account(books),
                cash_account=books.till,
                amount=Decimal("10.00"),
                method=PaymentMethod.CASH,
                date=local_today(),
                note="",
                by=books.owner,
            ).number
            == "O-000001"
        )
        assert next_number(books.company, "T-") == "T-000003"

        other, other_owner = make_firm("B")
        customer = make_customer(other, "Theirs")
        with company_context(other.id):
            theirs = SimpleNamespace(
                company=other,
                owner=other_owner,
                customer=customer,
                account=Account.objects.get(customer=customer),
                till=CashAccount.objects.get(),
            )
            assert receive(theirs).number == "T-000001"
        assert DocumentCounter.unscoped.get(company=books.company, prefix="T-").last == 3

    def test_applying_more_than_remaining_is_refused(self, books):
        invoice = make_invoice(books, "1000.00")
        receive(books, "600.00", applies=[(invoice, Decimal("600.00"))])
        assert invoice_collected(invoice) == Decimal("600.00")
        assert invoice_remaining(invoice) == Decimal("400.00")

        with pytest.raises(ValidationError) as error:
            receive(books, "500.00", applies=[(invoice, Decimal("400.01"))])
        assert codes(error) == {"applies": "OVERPAYMENT_ON_INVOICE"}
        assert Collection.objects.count() == 1
        assert Movement.objects.count() == 1

        receive(books, "400.00", applies=[(invoice, Decimal("400.00"))])
        assert invoice_remaining(invoice) == Decimal("0.00")

    def test_applications_must_fit_the_collection_and_its_customer(self, books):
        invoice = make_invoice(books, "1000.00")
        with pytest.raises(ValidationError) as error:
            receive(books, "100.00", applies=[(invoice, Decimal("100.01"))])
        assert codes(error) == {"applies": "invalid"}

        for refused in (
            make_invoice(books, status=InvoiceStatus.DRAFT),
            make_invoice(books, status=InvoiceStatus.CANCELLED),
            make_invoice(books, customer=make_customer(books.company, "Someone else")),
        ):
            with pytest.raises(ValidationError) as error:
                receive(books, "100.00", applies=[(refused, Decimal("100.00"))])
            assert codes(error) == {"applies": "invalid"}

        dollars = make_invoice(books, currency="USD")
        with pytest.raises(ValidationError) as error:
            receive(books, "100.00", applies=[(dollars, Decimal("100.00"))])
        assert codes(error) == {"applies": "CURRENCY_MISMATCH"}
        assert Collection.objects.count() == 0
        assert Movement.objects.count() == 0

    def test_a_cancelled_collection_frees_the_invoice(self, books):
        invoice = make_invoice(books, "1000.00")
        collection = receive(books, "1000.00", applies=[(invoice, Decimal("1000.00"))])
        assert invoice_remaining(invoice) == Decimal("0.00")

        cancel_collection(collection, by=books.owner)

        assert invoice_collected(invoice) == Decimal("0.00")
        assert invoice_remaining(invoice) == Decimal("1000.00")

    def test_an_unallocated_collection_is_allowed(self, books):
        collection = receive(books, "300.00")
        assert not CollectionApplication.objects.filter(collection=collection).exists()
        assert account_balance(books.account) == Decimal("-300.00")

        invoice = make_invoice(books, "1000.00")
        partly = receive(books, "500.00", applies=[(invoice, Decimal("200.00"))])
        [application] = CollectionApplication.objects.filter(collection=partly)
        assert (application.invoice, application.amount) == (invoice, Decimal("200.00"))

    def test_applies_lock_the_invoice(self, books):
        invoice = make_invoice(books)
        manager = InvoiceDraft.objects
        with patch.object(manager, "select_for_update", wraps=manager.select_for_update) as spy:
            receive(books, "100.00", applies=[(invoice, Decimal("100.00"))])
        assert spy.called

    def test_another_firms_records_are_refused(self, books):
        other, other_owner = make_firm("B")
        customer = make_customer(other, "Theirs")
        with system_context():
            their_account = Account.unscoped.get(customer=customer)
            their_till = CashAccount.unscoped.get(company=other)
            their_invoice = make_invoice(books, customer=customer)
        for overrides in ({"cash_account": their_till}, {"account": their_account}):
            with system_context(), pytest.raises(ValidationError) as error:
                receive(books, **overrides)
            assert set(codes(error).values()) == {"does_not_exist"}
        with system_context(), pytest.raises(ValidationError) as error:
            receive(books, applies=[(their_invoice, Decimal("100.00"))])
        assert codes(error) == {"applies": "does_not_exist"}
        with system_context(), pytest.raises(ValidationError) as error:
            record_transfer(
                kind=TransferKind.CASH,
                from_cash=books.till,
                to_cash=their_till,
                amount=Decimal("1.00"),
                date=local_today(),
                note="",
                by=books.owner,
            )
        assert codes(error) == {"to_cash": "does_not_exist"}
        with system_context():
            assert Movement.unscoped.count() == 0

    def test_payment_out_debits_and_empties_the_till(self, books):
        parts = supplier_account(books)
        payment = record_payment_out(
            account=parts,
            cash_account=books.till,
            amount=Decimal("75.00"),
            method=PaymentMethod.TRANSFER,
            date=local_today(),
            note="Parca",
            by=books.owner,
        )

        assert payment.number == "O-000001"
        [movement] = movements_of(payment)
        assert movement.source == "payment_out"
        assert (movement.account, movement.direction) == (parts, "debit")
        assert (movement.cash_account, movement.cash_direction) == (books.till, "out")
        assert account_balance(parts) == Decimal("75.00")
        assert cash_balance(books.till) == Decimal("-75.00")

    def test_manual_entry_moves_only_the_account(self, books):
        entry = record_manual_entry(
            account=books.account,
            direction=Direction.DEBIT,
            amount=Decimal("40.00"),
            date=local_today(),
            reason="Late fee",
            by=books.owner,
        )
        [movement] = movements_of(entry)
        assert movement.source == "manual"
        assert movement.cash_account is None
        assert account_balance(books.account) == Decimal("40.00")

        with pytest.raises(ValidationError) as error:
            record_manual_entry(
                account=books.account,
                direction=Direction.CREDIT,
                amount=Decimal("1.00"),
                date=local_today(),
                reason="  ",
                by=books.owner,
            )
        assert codes(error) == {"reason": "required"}

    def test_cash_transfer_moves_between_tills_only(self, books):
        bank = CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name="Bank")
        transfer = record_transfer(
            kind=TransferKind.CASH,
            from_cash=books.till,
            to_cash=bank,
            amount=Decimal("500.00"),
            date=local_today(),
            note="Deposit",
            by=books.owner,
        )

        moved = movements_of(transfer)
        assert len(moved) == 2
        assert all(m.account is None and m.direction == "" for m in moved)
        assert all(m.source == "transfer" for m in moved)
        assert cash_balance(books.till) == Decimal("-500.00")
        assert cash_balance(bank) == Decimal("500.00")
        assert account_balance(books.account) == Decimal("0.00")

        with pytest.raises(ValidationError):
            record_transfer(
                kind=TransferKind.CASH,
                from_cash=bank,
                to_cash=bank,
                amount=Decimal("1.00"),
                date=local_today(),
                note="",
                by=books.owner,
            )

    def test_account_transfer_moves_a_balance(self, books):
        related = Account.objects.get(customer=make_customer(books.company, "Sister Co"))
        record_transfer(
            kind=TransferKind.ACCOUNT,
            from_account=books.account,
            to_account=related,
            amount=Decimal("120.00"),
            date=local_today(),
            note="",
            by=books.owner,
        )
        assert account_balance(books.account) == Decimal("-120.00")
        assert account_balance(related) == Decimal("120.00")
        assert cash_balance(books.till) == Decimal("0.00")

    def test_account_transfer_needs_one_currency(self, books):
        dollars = Account.objects.get(customer=make_customer(books.company, "Dollar Co"))
        Account.objects.filter(pk=dollars.pk).update(currency="USD")
        dollars.refresh_from_db()

        with pytest.raises(ValidationError) as error:
            record_transfer(
                kind=TransferKind.ACCOUNT,
                from_account=books.account,
                to_account=dollars,
                amount=Decimal("10.00"),
                date=local_today(),
                note="",
                by=books.owner,
            )
        assert codes(error) == {"to_account": "CURRENCY_MISMATCH"}
        assert Transfer.objects.count() == 0
        assert Movement.objects.count() == 0

    def test_opening_only_before_other_movements(self, books):
        today = local_today()
        yesterday = today - timedelta(days=1)
        last_week = today - timedelta(days=7)

        def open_on(day: date, amount: str = "900.00", **side: Any) -> OpeningBalance:
            return record_opening(
                **(side or {"account": books.account}),
                direction=Direction.DEBIT,
                amount=Decimal(amount),
                date=day,
                by=books.owner,
            )

        # An invoice already posted yesterday: an opening dated before it is
        # accepted, which is what the M7 migration's invoices need.
        collect(
            books,
            "1000.00",
            cash_account=None,
            cash_direction=None,
            direction=Direction.DEBIT,
            source=MovementSource.INVOICE,
            date=yesterday,
        )
        opening = open_on(last_week)
        [movement] = movements_of(opening)
        assert movement.source == "opening"
        assert account_balance(books.account) == Decimal("1900.00")

        # One live opening per side.
        with pytest.raises(ValidationError) as error:
            open_on(last_week, "1.00")
        assert codes(error) == {"account": "unique"}

        # Once open, nothing is dated before the opening, by any writer.
        with pytest.raises(ValidationError) as error:
            receive(books, "10.00", date=last_week - timedelta(days=1))
        assert codes(error) == {"date": "invalid"}
        assert "Earlier than the opening balance" in str(error.value)
        receive(books, "10.00", date=last_week)  # the same day is fine

        # Reversed, it can be entered again, on or before the earliest
        # movement still standing, and not after it.
        cancel_opening(opening, by=books.owner)
        with pytest.raises(ValidationError) as error:
            open_on(today)
        assert codes(error) == {"date": "invalid"}
        assert "Opening balance must come first" in str(error.value)
        open_on(last_week, "950.00")
        assert account_balance(books.account) == Decimal("1940.00")

        # The same on a cash account: after the till's first movement, refused.
        with pytest.raises(ValidationError) as error:
            open_on(today, "5.00", cash_account=books.till)
        assert codes(error) == {"date": "invalid"}
        assert OpeningBalance.objects.filter(cash_account=books.till).count() == 0

    def test_a_cash_opening_debit_fills_the_till(self, books):
        record_opening(
            cash_account=books.till,
            direction=Direction.DEBIT,
            amount=Decimal("250.00"),
            date=local_today(),
            by=books.owner,
        )
        assert cash_balance(books.till) == Decimal("250.00")

    def test_cancel_posts_reversals_and_only_once(self, books):
        bank = CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name="Bank")
        parts = supplier_account(books)
        today = local_today()
        documents: list[tuple[Any, Callable[..., Any]]] = [
            (receive(books, "100.00"), cancel_collection),
            (
                record_payment_out(
                    account=parts,
                    cash_account=books.till,
                    amount=Decimal("30.00"),
                    method=PaymentMethod.CARD,
                    date=today,
                    note="",
                    by=books.owner,
                ),
                cancel_payment_out,
            ),
            (
                record_manual_entry(
                    account=books.account,
                    direction=Direction.DEBIT,
                    amount=Decimal("15.00"),
                    date=today,
                    reason="Fee",
                    by=books.owner,
                ),
                cancel_manual_entry,
            ),
            (
                record_transfer(
                    kind=TransferKind.CASH,
                    from_cash=books.till,
                    to_cash=bank,
                    amount=Decimal("20.00"),
                    date=today,
                    note="",
                    by=books.owner,
                ),
                cancel_transfer,
            ),
            (
                record_opening(
                    account=supplier_account(books, "Opened Co"),
                    direction=Direction.CREDIT,
                    amount=Decimal("500.00"),
                    date=today,
                    by=books.owner,
                ),
                cancel_opening,
            ),
        ]

        for document, cancel in documents:
            originals = movements_of(document)
            cancelled = cancel(document, by=books.owner)
            assert cancelled.cancelled_at is not None
            moved = movements_of(document)
            assert len(moved) == 2 * len(originals)
            reversals = [m for m in moved if m.source == "reversal"]
            assert {m.reverses_id for m in reversals} == {m.pk for m in originals}

            with pytest.raises(Conflict) as error:
                cancel(document, by=books.owner)
            assert codes(error) == "ALREADY_REVERSED"
            assert len(movements_of(document)) == 2 * len(originals)

        for side in Account.objects.all():
            assert account_balance(side) == Decimal("0.00")
        for till in (books.till, bank):
            assert cash_balance(till) == Decimal("0.00")


class Boom(RuntimeError):
    """A failure injected midway through a service."""


def fail_on_call(real: Callable[..., Any], n: int) -> Callable[..., Any]:
    """`real`, except that its n-th call raises instead."""
    calls = {"count": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == n:
            raise Boom
        return real(*args, **kwargs)

    return wrapper


def ledger_state(books: SimpleNamespace, invoices: list[InvoiceDraft]) -> dict[str, Any]:
    return {
        "movements": Movement.objects.count(),
        "collections": Collection.objects.count(),
        "cancelled": Collection.objects.filter(cancelled_at__isnull=False).count(),
        "applications": CollectionApplication.objects.count(),
        "transfers": Transfer.objects.count(),
        "counters": {
            c.prefix: c.last for c in DocumentCounter.unscoped.filter(company=books.company)
        },
        "invoices": {
            i.pk: (i.status, i.paid_on)
            for i in InvoiceDraft.objects.filter(pk__in=[i.pk for i in invoices])
        },
    }


class TestAtomicity:
    @pytest.mark.parametrize("operation", ["collection", "transfer", "cancel_collection"])
    def test_a_failure_midway_leaves_nothing(self, books, operation):
        first = make_invoice(books, "1000.00")
        second = make_invoice(books, "500.00")
        bank = CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name="Bank")
        # Something on the books already, so the counter row exists and a
        # rolled-back increment would show as a gap.
        standing = receive(books, "50.00", applies=[(first, Decimal("50.00"))])

        def collect_twice() -> Any:
            return receive(
                books,
                "300.00",
                applies=[(first, Decimal("100.00")), (second, Decimal("200.00"))],
            )

        def transfer() -> Any:
            return record_transfer(
                kind=TransferKind.CASH,
                from_cash=books.till,
                to_cash=bank,
                amount=Decimal("20.00"),
                date=local_today(),
                note="",
                by=books.owner,
            )

        def cancel() -> Any:
            return cancel_collection(standing, by=books.owner)

        run, target, failing_call = {
            # The collection's one posting comes after both applications.
            "collection": (collect_twice, "post", 1),
            # The second of the transfer's two legs.
            "transfer": (transfer, "post", 2),
            "cancel_collection": (cancel, "reverse_document", 1),
        }[operation]
        before = ledger_state(books, [first, second])

        real = getattr(ledger_services, target)
        with (
            patch.object(ledger_services, target, side_effect=fail_on_call(real, failing_call)),
            pytest.raises(Boom),
        ):
            run()

        assert ledger_state(books, [first, second]) == before
        assert invoice_remaining(first) == Decimal("950.00")
        assert invoice_remaining(second) == Decimal("500.00")

        # The next attempt succeeds, and the counter shows no gap.
        result = run()
        if operation == "collection":
            assert result.number == "T-000002"
        else:
            assert receive(books).number == "T-000002"

    def test_a_reversal_failing_midway_leaves_no_mirror(self, books):
        bank = CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name="Bank")
        transfer = record_transfer(
            kind=TransferKind.CASH,
            from_cash=books.till,
            to_cash=bank,
            amount=Decimal("20.00"),
            date=local_today(),
            note="",
            by=books.owner,
        )
        assert len(movements_of(transfer)) == 2

        manager = Movement.objects
        with (
            patch.object(manager, "create", side_effect=fail_on_call(manager.create, 2)),
            pytest.raises(Boom),
        ):
            reverse_document(transfer, date=local_today(), by=books.owner)

        assert not Movement.objects.filter(source=MovementSource.REVERSAL).exists()
        assert len(movements_of(transfer)) == 2
        assert cash_balance(bank) == Decimal("20.00")

        # And the document can still be cancelled, whole.
        cancel_transfer(transfer, by=books.owner)
        assert Movement.objects.filter(source=MovementSource.REVERSAL).count() == 2


class TestPostingGuards:
    def test_post_locks_its_sides(self, books):
        locked: list[tuple[type, Any]] = []
        for model in (Account, CashAccount):
            manager = model.unscoped
            real = manager.select_for_update

            def spy(*args: Any, _model: type = model, _real: Any = real, **kwargs: Any) -> Any:
                queryset = _real(*args, **kwargs)
                locked.append((_model, queryset))
                return queryset

            patcher = patch.object(manager, "select_for_update", side_effect=spy)
            patcher.start()
        try:
            collect(books)
        finally:
            patch.stopall()

        locked_rows = {(model, row.pk) for model, queryset in locked for row in queryset}
        assert (Account, books.account.pk) in locked_rows
        assert (CashAccount, books.till.pk) in locked_rows

    @pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
    def test_amounts_that_are_not_numbers_are_a_field_error(self, books, amount):
        invoice = make_invoice(books)
        bad = Decimal(amount)
        attempts: list[Callable[[], Any]] = [
            lambda: post(
                account=books.account,
                cash_account=books.till,
                direction=Direction.CREDIT,
                cash_direction=CashDirection.IN,
                amount=bad,
                currency="TRY",
                date=local_today(),
                source=MovementSource.COLLECTION,
                document=None,
                description="",
                by=books.owner,
            ),
            lambda: receive(books, amount=amount),
            lambda: record_manual_entry(
                account=books.account,
                direction=Direction.DEBIT,
                amount=bad,
                date=local_today(),
                reason="Fee",
                by=books.owner,
            ),
        ]
        for attempt in attempts:
            with pytest.raises(ValidationError) as error:
                attempt()
            assert codes(error) == {"amount": "invalid"}
        with pytest.raises(ValidationError) as error:
            receive(books, "100.00", applies=[(invoice, bad)])
        assert codes(error) == {"applies": "invalid"}
        assert Movement.objects.count() == 0


# M7 invoices on the books ---------------------------------------------------------


def issued_through_m7(books: SimpleNamespace, number: str = "GIB-1") -> InvoiceDraft:
    """A draft with one priced line, issued through M7: 1000.00 + 20% VAT."""
    draft = make_invoice(books, status=InvoiceStatus.DRAFT)
    billing.add_line(
        draft,
        description="Bakim",
        quantity=Decimal("1"),
        unit_price=Decimal("1000.00"),
        by=books.owner,
    )
    return billing.issue(
        draft,
        invoice_number=number,
        invoice_date=local_today() - timedelta(days=5),
        by=books.owner,
    )


def fresh(draft: InvoiceDraft) -> InvoiceDraft:
    return InvoiceDraft.objects.get(pk=draft.pk)


class TestInvoices:
    def test_issuing_posts_one_debit(self, books):
        draft = issued_through_m7(books)

        (debit,) = movements_of(draft)
        assert debit.source == MovementSource.INVOICE
        assert (debit.account, debit.direction) == (books.account, Direction.DEBIT)
        assert debit.cash_account is None
        assert debit.amount == Decimal("1200.00")
        assert debit.date == draft.invoice_date
        assert debit.description == "Fatura GIB-1 — 01.09.2026-30.09.2026"
        assert account_balance(books.account) == Decimal("1200.00")

    def test_reissue_or_retry_posts_nothing_more(self, books):
        issued = issued_through_m7(books)
        assert ledger_services.post_invoice(issued, by=books.owner) is None

        # One issued before the ledger: the first call posts, the second does not.
        legacy = make_invoice(books, "500.00")
        assert ledger_services.post_invoice(legacy, by=books.owner) is not None
        assert ledger_services.post_invoice(legacy, by=books.owner) is None

        assert len(movements_of(issued)) == 1
        assert len(movements_of(legacy)) == 1
        assert account_balance(books.account) == Decimal("1700.00")

    def test_cancelling_an_issued_invoice_reverses_it(self, books):
        draft = issued_through_m7(books)

        billing.cancel(draft, reason="Wrong amount", by=books.owner)

        debit, credit = movements_of(draft)
        assert credit.source == MovementSource.INVOICE_CANCEL
        assert credit.reverses == debit
        assert (credit.direction, credit.amount) == (Direction.CREDIT, Decimal("1200.00"))
        assert credit.date == local_today()
        assert account_balance(books.account) == Decimal("0.00")
        assert fresh(draft).status == InvoiceStatus.CANCELLED

        # Once only: the reversal is refused, and the database would refuse it too.
        with pytest.raises(Conflict) as error:
            ledger_services.reverse_invoice(draft, by=books.owner)
        assert codes(error) == "ALREADY_REVERSED"
        with pytest.raises(Conflict):
            reverse_document(
                draft, date=local_today(), by=books.owner, source=MovementSource.INVOICE_CANCEL
            )
        assert len(movements_of(draft)) == 2

    def test_a_reversal_is_never_posted_directly(self, books):
        for source in (MovementSource.REVERSAL, "nonsense"):
            with pytest.raises(ValidationError) as error:
                collect(books, source=source)
            assert codes(error) == {"source": "invalid"}
        with pytest.raises(ValidationError):
            reverse_document(
                books.customer, date=local_today(), by=books.owner, source=MovementSource.MANUAL
            )

    def test_cancelling_with_collections_is_refused(self, books):
        draft = issued_through_m7(books)
        receive(books, "200.00", applies=[(draft, Decimal("200.00"))])

        with pytest.raises(Conflict) as error:
            billing.cancel(draft, reason="Wrong amount", by=books.owner)

        assert codes(error) == "INVOICE_HAS_COLLECTIONS"
        assert fresh(draft).status == InvoiceStatus.ISSUED
        assert len(movements_of(draft)) == 1

    def test_partial_collection_leaves_it_issued_with_remaining(self, books):
        draft = issued_through_m7(books)

        receive(books, "500.00", applies=[(draft, Decimal("500.00"))])

        draft = fresh(draft)
        assert (draft.status, draft.paid_on) == (InvoiceStatus.ISSUED, None)
        assert invoice_remaining(draft) == Decimal("700.00")
        body = api_for(books.owner).get(reverse("invoice-draft-detail", args=[draft.pk])).data
        assert (body["collected"], body["remaining"]) == ("500.00", "700.00")
        listed = api_for(books.owner).get(reverse("invoice-draft-list")).data["results"]
        assert [(row["collected"], row["remaining"]) for row in listed] == [("500.00", "700.00")]

    def test_the_covering_collection_marks_it_paid(self, books):
        draft = issued_through_m7(books)
        covering_day = local_today() - timedelta(days=1)

        receive(
            books,
            "500.00",
            applies=[(draft, Decimal("500.00"))],
            date=local_today() - timedelta(days=3),
        )
        receive(books, "700.00", applies=[(draft, Decimal("700.00"))], date=covering_day)

        draft = fresh(draft)
        assert (draft.status, draft.paid_on) == (InvoiceStatus.PAID, covering_day)
        assert invoice_remaining(draft) == Decimal("0.00")

    def test_cancelling_a_collection_reopens_the_invoice(self, books):
        draft = issued_through_m7(books)
        first = receive(books, "500.00", applies=[(draft, Decimal("500.00"))])
        second = receive(
            books,
            "700.00",
            applies=[(draft, Decimal("700.00"))],
            date=local_today() - timedelta(days=1),
        )

        cancel_collection(first, by=books.owner)
        draft = fresh(draft)
        assert (draft.status, draft.paid_on) == (InvoiceStatus.ISSUED, None)
        assert invoice_remaining(draft) == Decimal("500.00")

        cancel_collection(second, by=books.owner)
        draft = fresh(draft)
        assert (draft.status, draft.paid_on) == (InvoiceStatus.ISSUED, None)
        assert invoice_remaining(draft) == Decimal("1200.00")

        # Paid in full again, then cancelled as an invoice: refused until the
        # collection goes first, after which it reverses.
        third = receive(books, "1200.00", applies=[(draft, Decimal("1200.00"))])
        assert fresh(draft).status == InvoiceStatus.PAID
        cancel_collection(third, by=books.owner)
        billing.cancel(fresh(draft), reason="Wrong amount", by=books.owner)
        assert account_balance(books.account) == Decimal("0.00")

    def test_a_cancelled_invoice_is_never_touched(self, books):
        draft = issued_through_m7(books)
        before = fresh(draft)
        billing.cancel(draft, reason="x", by=books.owner)
        cancelled = fresh(draft)

        assert ledger_services.sync_invoice_status(cancelled).status == InvoiceStatus.CANCELLED
        assert fresh(draft).paid_on == before.paid_on

    def test_mark_paid_is_a_full_collection_into_merkez_kasa(self, books):
        draft = issued_through_m7(books)
        receive(
            books,
            "200.00",
            applies=[(draft, Decimal("200.00"))],
            date=local_today() - timedelta(days=3),
        )
        paid_on = local_today() - timedelta(days=1)

        draft = billing.mark_paid(draft, paid_on=paid_on, by=books.owner)

        assert (draft.status, draft.paid_on) == (InvoiceStatus.PAID, paid_on)
        collection = Collection.objects.order_by("-number").first()
        assert collection is not None
        assert collection.cash_account == books.till
        assert books.till.name == DEFAULT_CASH_ACCOUNT_NAME
        assert (collection.amount, collection.method, collection.date) == (
            Decimal("1000.00"),
            PaymentMethod.TRANSFER,
            paid_on,
        )
        (application,) = CollectionApplication.objects.filter(collection=collection)
        assert (application.invoice_id, application.amount) == (draft.pk, Decimal("1000.00"))
        assert cash_balance(books.till) == Decimal("1200.00")
        assert account_balance(books.account) == Decimal("0.00")

        # Paid is paid: a second mark-paid is refused.
        with pytest.raises(Conflict):
            billing.mark_paid(draft, paid_on=paid_on, by=books.owner)

    def test_the_deprecated_endpoint_collects_into_the_cash_account_given(self, books):
        draft = issued_through_m7(books)
        bank = CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name="Bank")

        response = api_for(books.owner).post(
            reverse("invoice-draft-mark-paid", args=[draft.pk]),
            {"paid_on": str(local_today()), "cash_account": str(bank.pk), "method": "card"},
        )

        assert response.status_code == 200, response.data
        assert (response.data["status"], response.data["remaining"]) == (
            InvoiceStatus.PAID,
            "0.00",
        )
        (collection,) = Collection.objects.all()
        assert (collection.cash_account, collection.method) == (bank, PaymentMethod.CARD)
        assert cash_balance(bank) == Decimal("1200.00")

    @pytest.mark.parametrize(
        ("operation", "target"),
        [
            ("issue", "post"),
            ("cancel", "reverse_document"),
            ("mark_paid", "post"),
            ("mark_paid", "sync_invoice_status"),
        ],
    )
    def test_invoice_operations_are_all_or_nothing(self, books, operation, target):
        issued = issued_through_m7(books)
        # Something on the books already, so the counter row exists and a
        # rolled-back increment would show.
        receive(books, "50.00")
        draft = make_invoice(books, status=InvoiceStatus.DRAFT)
        billing.add_line(
            draft,
            description="Bakim",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            by=books.owner,
        )
        subject = draft if operation == "issue" else issued

        def state() -> dict[str, Any]:
            row = fresh(subject)
            return {
                "draft": (row.status, row.invoice_number, row.paid_on, row.cancelled_at),
                "movements": Movement.objects.count(),
                "collections": Collection.objects.count(),
                "applications": CollectionApplication.objects.count(),
                "counters": {
                    c.prefix: c.last for c in DocumentCounter.unscoped.filter(company=books.company)
                },
            }

        run = {
            "issue": lambda: billing.issue(
                subject, invoice_number="GIB-2", invoice_date=local_today(), by=books.owner
            ),
            "cancel": lambda: billing.cancel(subject, reason="x", by=books.owner),
            "mark_paid": lambda: billing.mark_paid(subject, paid_on=local_today(), by=books.owner),
        }[operation]
        before = state()

        with patch.object(ledger_services, target, side_effect=Boom), pytest.raises(Boom):
            run()

        assert state() == before
        # And it goes through whole once the ledger answers.
        run()
        assert state() != before


class TestInvoiceMigration:
    def test_the_migration_posts_existing_drafts_once(self, books):
        issued = make_invoice(books, "1000.00")
        paid = make_invoice(books, "500.00", status=InvoiceStatus.PAID)
        cancelled = make_invoice(books, "300.00", status=InvoiceStatus.CANCELLED)
        draft = make_invoice(books, "200.00", status=InvoiceStatus.DRAFT)
        with system_context():
            other, _ = make_firm("B")
        their_customer = make_customer(other, "Theirs")
        theirs = make_invoice(books, "800.00", status=InvoiceStatus.PAID, customer=their_customer)
        InvoiceDraft.unscoped.filter(pk=theirs.pk).update(paid_on=date(2026, 10, 3))

        migration = importlib.import_module("apps.ledger.migrations.0008_carry_over_invoices")
        with system_context():
            migration.carry_over(global_apps, None)
            first = carry_over_invoices()
        assert (first.posted, first.collected) == (0, 0)

        with system_context():
            debits = Movement.unscoped.filter(source=MovementSource.INVOICE)
            assert set(debits.values_list("document_id", flat=True)) == {
                issued.pk,
                paid.pk,
                theirs.pk,
            }
            assert debits.count() == 3
            assert not Movement.unscoped.filter(document_id__in=[cancelled.pk, draft.pk]).exists()

            devir = CashAccount.unscoped.filter(role=CashAccountRole.CARRIED_OVER)
            assert sorted(devir.values_list("company_id", "name", "kind")) == sorted(
                [
                    (books.company.id, "Devir (taşınan ödemeler)", CashKind.CASH),
                    (other.id, "Devir (taşınan ödemeler)", CashKind.CASH),
                ]
            )
            collections = Collection.unscoped.order_by("date")
            assert [
                (c.company_id, c.amount, c.date, c.method, c.note, c.cash_account.role)
                for c in collections
            ] == [
                (
                    books.company.id,
                    Decimal("500.00"),
                    date(2026, 10, 1),
                    PaymentMethod.TRANSFER,
                    "M7'den taşındı",
                    CashAccountRole.CARRIED_OVER,
                ),
                (
                    other.id,
                    Decimal("800.00"),
                    date(2026, 10, 3),
                    PaymentMethod.TRANSFER,
                    "M7'den taşındı",
                    CashAccountRole.CARRIED_OVER,
                ),
            ]
            assert sorted(
                CollectionApplication.unscoped.values_list("invoice_id", "amount")
            ) == sorted([(paid.pk, Decimal("500.00")), (theirs.pk, Decimal("800.00"))])
            statuses = dict(
                InvoiceDraft.unscoped.filter(pk__in=[issued.pk, paid.pk, theirs.pk]).values_list(
                    "pk", "status"
                )
            )
        assert statuses == {
            issued.pk: InvoiceStatus.ISSUED,
            paid.pk: InvoiceStatus.PAID,
            theirs.pk: InvoiceStatus.PAID,
        }
        assert account_balance(books.account) == Decimal("1000.00")
        assert invoice_remaining(fresh(paid)) == Decimal("0.00")
        assert fresh(paid).paid_on == date(2026, 10, 1)


class TestInvoiceGuarantees:
    """Fix round 1: every invoice path is all or nothing, the default till is
    found by role, a zero invoice is paid, and a raced mark-paid is a 409."""

    def test_a_cancel_failing_after_its_reversal_leaves_nothing(self, books):
        draft = issued_through_m7(books)
        before = (fresh(draft).status, account_balance(books.account))

        # The mirror is written, then the draft's own save fails.
        with patch.object(InvoiceDraft, "save", side_effect=Boom), pytest.raises(Boom):
            billing.cancel(draft, reason="x", by=books.owner)

        assert not Movement.objects.filter(source=MovementSource.INVOICE_CANCEL).exists()
        assert (fresh(draft).status, account_balance(books.account)) == before
        # And it cancels whole on the next try.
        billing.cancel(draft, reason="x", by=books.owner)
        assert Movement.objects.filter(source=MovementSource.INVOICE_CANCEL).count() == 1

    @pytest.mark.parametrize("amount", ["1200.00", "500.00"], ids=["covering", "partial"])
    def test_a_collection_cancel_failing_after_its_reversal_leaves_nothing(self, books, amount):
        draft = issued_through_m7(books)
        collection = receive(
            books,
            amount,
            applies=[(draft, Decimal(amount))],
            date=local_today() - timedelta(days=1),
        )
        paid_state = (fresh(draft).status, fresh(draft).paid_on)
        balances = (account_balance(books.account), cash_balance(books.till))

        with (
            patch.object(ledger_services, "sync_invoice_status", side_effect=Boom),
            pytest.raises(Boom),
        ):
            cancel_collection(collection, by=books.owner)

        assert not Movement.objects.filter(source=MovementSource.REVERSAL).exists()
        assert Collection.objects.get(pk=collection.pk).cancelled_at is None
        assert (fresh(draft).status, fresh(draft).paid_on) == paid_state
        assert (account_balance(books.account), cash_balance(books.till)) == balances

    def test_a_zero_invoice_is_paid_on_issue(self, books):
        draft = make_invoice(books, status=InvoiceStatus.DRAFT)
        billing.add_line(
            draft,
            description="Ucretsiz",
            quantity=Decimal("1"),
            unit_price=Decimal("0.00"),
            by=books.owner,
        )
        invoice_date = local_today() - timedelta(days=2)

        issued = billing.issue(
            draft, invoice_number="GIB-0", invoice_date=invoice_date, by=books.owner
        )

        assert (issued.status, issued.paid_on) == (InvoiceStatus.PAID, invoice_date)
        assert fresh(draft).status == InvoiceStatus.PAID
        assert movements_of(draft) == []
        with pytest.raises(Conflict) as error:
            billing.mark_paid(draft, paid_on=local_today(), by=books.owner)
        assert codes(error) == "INVOICE_TRANSITION_NOT_ALLOWED"

    def test_mark_paid_uses_the_renamed_default_till(self, books):
        books.till.name = "Ana kasa"
        books.till.save()
        draft = issued_through_m7(books)

        billing.mark_paid(draft, paid_on=local_today(), by=books.owner)

        assert CashAccount.objects.count() == 1
        (collection,) = Collection.objects.all()
        assert collection.cash_account == books.till
        assert books.till.role == CashAccountRole.DEFAULT
        assert ledger_services.create_default_cash_account(books.company) == books.till
        assert CashAccount.objects.count() == 1

    def test_the_migration_marks_existing_merkez_kasa_as_default(self, firm):
        company, _ = firm
        other, _ = make_firm("B")
        with system_context():
            # As 0002 left them: found by name, with no role.
            CashAccount.unscoped.update(role="")
            CashAccount.objects.create(company=company, kind=CashKind.BANK, name="Bank")
        migration = importlib.import_module("apps.ledger.migrations.0006_default_till_role")

        with system_context():
            migration.mark_default_tills(global_apps, None)
            migration.mark_default_tills(global_apps, None)
            defaults = sorted(
                CashAccount.unscoped.filter(role=CashAccountRole.DEFAULT).values_list(
                    "company_id", "name"
                )
            )
        assert defaults == sorted([(company.id, "Merkez kasa"), (other.id, "Merkez kasa")])

    def test_mark_paid_after_a_payment_raced_in_is_a_409(self, books):
        draft = issued_through_m7(books)
        real_locked = billing._locked

        def another_payment_commits_first(d: InvoiceDraft) -> InvoiceDraft:
            # What a concurrent POST /collections/ looks like from here: it
            # covered the invoice and committed before this request's lock.
            receive(books, "1200.00", applies=[(draft, Decimal("1200.00"))])
            return real_locked(d)

        with (
            patch.object(billing, "_locked", side_effect=another_payment_commits_first),
            pytest.raises(Conflict) as error,
        ):
            billing.mark_paid(draft, paid_on=local_today(), by=books.owner)

        # Not `400 {"applies": OVERPAYMENT_ON_INVOICE}`: the same answer a
        # second mark-paid in sequence gets. (The injected payment ran inside
        # this request's transaction here, so it rolled back with it.)
        assert codes(error) == "INVOICE_TRANSITION_NOT_ALLOWED"


LOCKED_AFTER_INVOICES = frozenset(
    {"Collection", "DocumentCounter", "Movement", "Account", "CashAccount"}
)


class TestLockOrder:
    """Fix round 2: one lock order for every money path (apps.ledger.services,
    "LOCK ORDER"): invoices first, then the document, the counter, movements
    and the sides."""

    @pytest.mark.parametrize(
        "path", ["record_collection", "cancel_collection", "mark_paid", "cancel", "issue"]
    )
    def test_every_path_locks_invoices_first(self, books, path):
        issued = issued_through_m7(books)
        receive(books, "50.00")  # the counter row exists
        standing = receive(books, "200.00", applies=[(issued, Decimal("200.00"))])
        draft = make_invoice(books, status=InvoiceStatus.DRAFT)
        billing.add_line(
            draft,
            description="Bakim",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            by=books.owner,
        )
        draft_to_cancel = issued_through_m7(books, number="GIB-3")
        run = {
            "record_collection": lambda: receive(
                books, "300.00", applies=[(issued, Decimal("300.00"))]
            ),
            "cancel_collection": lambda: cancel_collection(standing, by=books.owner),
            "mark_paid": lambda: billing.mark_paid(issued, paid_on=local_today(), by=books.owner),
            "cancel": lambda: billing.cancel(draft_to_cancel, reason="x", by=books.owner),
            "issue": lambda: billing.issue(
                draft, invoice_number="GIB-2", invoice_date=local_today(), by=books.owner
            ),
        }[path]

        locks: list[str] = []
        real = QuerySet.select_for_update

        def spy(self: QuerySet[Any], *args: Any, **kwargs: Any) -> QuerySet[Any]:
            locks.append(self.model.__name__)
            return real(self, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            run()

        invoice_locks = [i for i, name in enumerate(locks) if name == "InvoiceDraft"]
        later_locks = [i for i, name in enumerate(locks) if name in LOCKED_AFTER_INVOICES]
        assert invoice_locks, locks
        assert later_locks, locks
        assert max(invoice_locks) < min(later_locks), locks

    def test_a_zero_invoice_can_be_cancelled(self, books):
        draft = make_invoice(books, status=InvoiceStatus.DRAFT)
        billing.add_line(
            draft,
            description="Ucretsiz",
            quantity=Decimal("1"),
            unit_price=Decimal("0.00"),
            by=books.owner,
        )
        billing.issue(draft, invoice_number="GIB-0", invoice_date=local_today(), by=books.owner)
        assert fresh(draft).status == InvoiceStatus.PAID

        billing.cancel(draft, reason="Issued by mistake", by=books.owner)

        assert fresh(draft).status == InvoiceStatus.CANCELLED
        assert movements_of(draft) == []

    def test_a_paid_invoice_with_collections_is_refused(self, books):
        draft = issued_through_m7(books)
        receive(books, "1200.00", applies=[(draft, Decimal("1200.00"))])
        assert fresh(draft).status == InvoiceStatus.PAID

        with pytest.raises(Conflict) as error:
            billing.cancel(draft, reason="x", by=books.owner)

        assert codes(error) == "INVOICE_HAS_COLLECTIONS"
        assert fresh(draft).status == InvoiceStatus.PAID
        assert len(movements_of(draft)) == 1


# The API --------------------------------------------------------------------------


def staff(company: Company, role: str, name: str | None = None) -> User:
    with company_context(company.id):
        return User.objects.create_user(
            email=f"{name or role}-{company.pk.hex[:6]}@example.com",
            password=PASSWORD,
            company=company,
            first_name=(name or role).title(),
            last_name="Staff",
            role=role,
            is_email_verified=True,
        )


def keyed(client: APIClient, url: str, body: dict[str, Any], key: str | None = None) -> Any:
    """POST with an Idempotency-Key, a fresh one unless `key` is given."""
    return client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY=key or uuid4().hex)


def error_of(response: Any) -> tuple[str, list[tuple[str, str]]]:
    error = response.data["error"]
    return error["code"], [(row.get("field", ""), row["code"]) for row in error.get("details", [])]


def owed(books: SimpleNamespace, customer: Customer, total: str, days_ago: int) -> InvoiceDraft:
    """An issued invoice of `customer`, `days_ago` old, posted to its account."""
    draft = make_invoice(books, total, customer=customer)
    draft.invoice_date = local_today() - timedelta(days=days_ago)
    draft.save(update_fields=["invoice_date", "updated_at"])
    ledger_services.post_invoice(draft, by=books.owner)
    return draft


def bank_of(books: SimpleNamespace, name: str = "Bank") -> CashAccount:
    return CashAccount.objects.create(company=books.company, kind=CashKind.BANK, name=name)


def _collection_body(books: SimpleNamespace) -> dict[str, Any]:
    return {
        "account": str(books.account.pk),
        "cash_account": str(books.till.pk),
        "amount": "100.00",
        "method": "cash",
    }


def _payment_out_body(books: SimpleNamespace) -> dict[str, Any]:
    return {
        "account": str(supplier_account(books).pk),
        "cash_account": str(books.till.pk),
        "amount": "40.00",
        "method": "transfer",
    }


def _manual_body(books: SimpleNamespace) -> dict[str, Any]:
    return {
        "account": str(books.account.pk),
        "direction": "debit",
        "amount": "25.00",
        "reason": "Late fee",
    }


def _transfer_body(books: SimpleNamespace) -> dict[str, Any]:
    return {
        "kind": "cash",
        "from_cash": str(books.till.pk),
        "to_cash": str(bank_of(books).pk),
        "amount": "10.00",
    }


def _opening_body(books: SimpleNamespace) -> dict[str, Any]:
    return {"account": str(books.account.pk), "direction": "debit", "amount": "500.00"}


DOCUMENTS: dict[str, Callable[[SimpleNamespace], dict[str, Any]]] = {
    "ledger-collection": _collection_body,
    "ledger-payment-out": _payment_out_body,
    "ledger-manual-entry": _manual_body,
    "ledger-transfer": _transfer_body,
    "ledger-opening": _opening_body,
}


def all_balances(books: SimpleNamespace) -> dict[str, Decimal]:
    rows = {f"account:{a.name}:{a.pk}": account_balance(a) for a in Account.objects.all()}
    rows.update({f"cash:{c.name}:{c.pk}": cash_balance(c) for c in CashAccount.objects.all()})
    return rows


class TestApi:
    def test_accounts_list_with_balance_and_totals_and_filters(self, books):
        alpha, beta = books.customer, make_customer(books.company, "Beta Asansor")
        idle = make_customer(books.company, "Idle Ltd")
        owed(books, alpha, "1000.00", days_ago=45)
        owed(books, beta, "500.00", days_ago=5)
        parts = supplier_account(books, "Parts Co")
        record_manual_entry(
            account=parts,
            direction=Direction.CREDIT,
            amount=Decimal("300.00"),
            date=local_today(),
            reason="Invoice from Parts Co",
            by=books.owner,
        )
        client = api_for(books.owner)
        url = reverse("ledger-account-list")

        def names(**params: Any) -> set[str]:
            response = client.get(url, params)
            assert response.status_code == 200, response.data
            return {row["name"] for row in response.data["results"]}

        listed = client.get(url).data
        balances = {row["name"]: row["balance"] for row in listed["results"]}
        assert balances == {
            "Customer": "1000.00",
            "Beta Asansor": "500.00",
            "Idle Ltd": "0.00",
            "Parts Co": "-300.00",
        }
        assert listed["totals"] == {
            "receivables": "1500.00",
            "payables": "300.00",
            "net": "1200.00",
        }
        assert listed["pagination"]["total"] == 4

        assert names(kind="supplier") == {"Parts Co"}
        assert names(search="beta") == {"Beta Asansor"}
        assert names(has_balance="true") == {"Customer", "Beta Asansor", "Parts Co"}
        assert names(has_balance="false") == {idle.legal_name}
        assert names(overdue="true", overdue_days=30) == {"Customer"}
        assert names(overdue="true", overdue_days=3) == {"Customer", "Beta Asansor"}
        # The totals follow the filters.
        assert client.get(url, {"kind": "customer"}).data["totals"]["payables"] == "0.00"

        detail = client.get(reverse("ledger-account-detail", args=[books.account.pk])).data
        assert detail["balance"] == "1000.00"
        assert str(detail["customer"]) == str(alpha.pk)
        assert detail["last_movement"]["source"] == MovementSource.INVOICE
        assert detail["last_movement"]["amount"] == "1000.00"

    def test_an_account_is_opened_for_a_staff_member_once(self, books):
        tech = staff(books.company, Role.TECHNICIAN)
        client = api_for(books.owner)
        url = reverse("ledger-account-list")

        response = client.post(url, {"kind": "staff", "user": str(tech.pk)}, format="json")
        assert response.status_code == 201, response.data
        assert (response.data["kind"], response.data["name"], response.data["balance"]) == (
            "staff",
            tech.full_name,
            "0.00",
        )
        again = client.post(url, {"kind": "staff", "user": str(tech.pk)}, format="json")
        assert again.status_code == 400
        assert error_of(again)[1] == [("user", "unique")]
        # A customer's account came with the customer.
        dup = client.post(url, {"kind": "customer", "customer": str(books.customer.pk)})
        assert error_of(dup)[1] == [("customer", "unique")]

    def test_a_supplier_comes_with_its_account_and_renames_it(self, books):
        client = api_for(books.owner)
        response = client.post(
            reverse("ledger-supplier-list"), {"name": "Kabin Parca", "phone": ""}, format="json"
        )
        assert response.status_code == 201, response.data
        account = Account.objects.get(pk=response.data["account"])
        assert (account.kind, account.name) == (AccountKind.SUPPLIER, "Kabin Parca")

        renamed = client.patch(
            reverse("ledger-supplier-detail", args=[response.data["id"]]),
            {"name": "Kabin Parca AS"},
            format="json",
        )
        assert renamed.status_code == 200, renamed.data
        account.refresh_from_db()
        assert account.name == "Kabin Parca AS"

    def test_a_retried_collection_posts_once(self, books):
        client = api_for(books.owner)
        url = reverse("ledger-collection-list")
        body = _collection_body(books)

        first = keyed(client, url, body, key="retry-1")
        second = keyed(client, url, body, key="retry-1")

        assert first.status_code == second.status_code == 201, first.data
        assert str(first.data["id"]) == str(second.data["id"])
        assert Collection.objects.count() == 1
        assert Movement.objects.filter(source=MovementSource.COLLECTION).count() == 1
        assert account_balance(books.account) == Decimal("-100.00")

        # And without a key it is refused: the retry the key exists for would
        # record the money twice.
        bare = client.post(url, body, format="json")
        assert bare.status_code == 400
        assert error_of(bare)[0] == "IDEMPOTENCY_KEY_REQUIRED"
        assert Collection.objects.count() == 1

    def test_a_collection_pays_the_invoices_it_names(self, books):
        draft = owed(books, books.customer, "1000.00", days_ago=10)
        client = api_for(books.owner)
        body = {**_collection_body(books), "amount": "1000.00"}
        body["applies"] = [{"invoice": str(draft.pk), "amount": "600.00"}]

        response = keyed(client, reverse("ledger-collection-list"), body)

        assert response.status_code == 201, response.data
        assert response.json()["applies"] == [{"invoice": str(draft.pk), "amount": "600.00"}]
        assert response.data["number"] == "T-000001"
        assert invoice_remaining(draft) == Decimal("400.00")

        over = keyed(
            client,
            reverse("ledger-collection-list"),
            {**body, "applies": [{"invoice": str(draft.pk), "amount": "400.01"}]},
        )
        assert over.status_code == 400
        assert error_of(over)[1] == [("applies", "OVERPAYMENT_ON_INVOICE")]
        assert Collection.objects.count() == 1

    @pytest.mark.parametrize("basename", sorted(DOCUMENTS))
    def test_document_create_and_cancel_round_trip(self, books, basename):
        client = api_for(books.owner)
        body = DOCUMENTS[basename](books)
        before = all_balances(books)

        created = keyed(client, reverse(f"{basename}-list"), body)
        assert created.status_code == 201, created.data
        assert created.data["cancelled_at"] is None
        assert all_balances(books) != before
        listed = client.get(reverse(f"{basename}-list")).data["results"]
        assert [str(row["id"]) for row in listed] == [str(created.data["id"])]

        cancel_url = reverse(f"{basename}-cancel", args=[created.data["id"]])
        cancelled = client.post(cancel_url)
        assert cancelled.status_code == 200, cancelled.data
        assert cancelled.data["cancelled_at"] is not None
        assert all_balances(books) == {key: before.get(key, Decimal("0.00")) for key in before} | {
            key: Decimal("0.00") for key in set(all_balances(books)) - set(before)
        }

        again = client.post(cancel_url)
        assert again.status_code == 409
        assert error_of(again)[0] == "ALREADY_REVERSED"

    def test_no_route_edits_or_deletes_a_document(self, books):
        client = api_for(books.owner)
        created = keyed(client, reverse("ledger-collection-list"), _collection_body(books))
        url = reverse("ledger-collection-detail", args=[created.data["id"]])
        assert client.patch(url, {"amount": "1.00"}, format="json").status_code == 405
        assert client.delete(url).status_code == 405
        assert Collection.objects.get().amount == Decimal("100.00")

    def test_permissions(self, books):
        roles = {role: staff(books.company, role) for role in Role.values if role != Role.OWNER}
        roles[Role.OWNER] = books.owner
        tech = api_for(roles[Role.TECHNICIAN])
        reads = [
            reverse("ledger-account-list"),
            reverse("ledger-account-detail", args=[books.account.pk]),
            reverse("ledger-cash-account-list"),
            reverse("ledger-cash-account-movements", args=[books.till.pk]),
            reverse("ledger-supplier-list"),
            reverse("ledger-finance-summary"),
            *(reverse(f"{basename}-list") for basename in DOCUMENTS),
        ]
        for url in reads:
            assert tech.get(url).status_code == 403, url
        for basename, body in DOCUMENTS.items():
            response = keyed(tech, reverse(f"{basename}-list"), body(books))
            assert response.status_code == 403, basename
        assert (
            tech.post(
                reverse("ledger-cash-account-list"), {"kind": "cash", "name": "X"}
            ).status_code
            == 403
        )
        standing = receive(books, "5.00")
        assert tech.post(reverse("ledger-collection-cancel", args=[standing.pk])).status_code == 403
        assert Collection.objects.get(pk=standing.pk).cancelled_at is None
        parts = Supplier.objects.create(company=books.company, name="Parts")
        supplier_url = reverse("ledger-supplier-detail", args=[parts.pk])
        till_url = reverse("ledger-cash-account-detail", args=[books.till.pk])
        assert tech.post(reverse("ledger-supplier-list"), {"name": "Y"}).status_code == 403
        assert tech.patch(supplier_url, {"name": "Y"}, format="json").status_code == 403
        assert tech.patch(till_url, {"name": "Y"}, format="json").status_code == 403

        ops = api_for(roles[Role.OPERATIONS])
        made = keyed(ops, reverse("ledger-collection-list"), _collection_body(books))
        assert made.status_code == 201, made.data
        # What it records against, and nothing more of the ledger.
        assert ops.get(reverse("ledger-cash-account-list")).status_code == 200
        assert ops.get(till_url).status_code == 200
        assert ops.get(reverse("ledger-supplier-list")).status_code == 200
        assert ops.get(reverse("ledger-collection-list")).status_code == 403
        assert (
            ops.get(reverse("ledger-cash-account-movements", args=[books.till.pk])).status_code
            == 403
        )
        assert ops.get(reverse("ledger-finance-summary")).status_code == 403
        assert ops.get(reverse("ledger-settings")).status_code == 403
        assert ops.patch(till_url, {"name": "Kasa"}, format="json").status_code == 403
        assert ops.post(reverse("ledger-supplier-list"), {"name": "Ops Parts"}).status_code == 201
        assert ops.patch(supplier_url, {"name": "Parts AS"}, format="json").status_code == 200
        assert (
            keyed(ops, reverse("ledger-payment-out-list"), _payment_out_body(books)).status_code
            == 201
        )
        assert (
            keyed(ops, reverse("ledger-manual-entry-list"), _manual_body(books)).status_code == 403
        )
        assert keyed(ops, reverse("ledger-transfer-list"), _transfer_body(books)).status_code == 403
        assert ops.get(reverse("ledger-account-list")).status_code == 403

        admin = api_for(roles[Role.ADMIN])
        assert keyed(admin, reverse("ledger-opening-list"), _opening_body(books)).status_code == 403
        assert (
            keyed(admin, reverse("ledger-manual-entry-list"), _manual_body(books)).status_code
            == 403
        )
        assert (
            keyed(admin, reverse("ledger-collection-list"), _collection_body(books)).status_code
            == 201
        )
        assert admin.get(reverse("ledger-finance-summary")).status_code == 200
        assert admin.patch(till_url, {"name": "Kasa"}, format="json").status_code == 403
        assert admin.get(reverse("ledger-settings")).status_code == 403

        accountant = api_for(roles[Role.ACCOUNTANT])
        for basename, body in DOCUMENTS.items():
            response = keyed(accountant, reverse(f"{basename}-list"), body(books))
            assert response.status_code == 201, (basename, response.data)
            assert (
                accountant.post(
                    reverse(f"{basename}-cancel", args=[response.data["id"]])
                ).status_code
                == 200
            )
        for url in reads:
            assert accountant.get(url).status_code == 200, url
        made_cash = accountant.post(
            reverse("ledger-cash-account-list"),
            {"kind": "bank", "name": "Is Bankasi"},
            format="json",
        )
        assert made_cash.status_code == 201, made_cash.data
        assert accountant.patch(till_url, {"name": "Kasa"}, format="json").status_code == 200
        assert accountant.patch(supplier_url, {"notes": "x"}, format="json").status_code == 200
        assert accountant.get(reverse("ledger-settings")).status_code == 200
        assert (
            accountant.post(
                reverse("ledger-account-list"),
                {"kind": "staff", "user": str(roles[Role.TECHNICIAN].pk)},
                format="json",
            ).status_code
            == 201
        )

    def test_cash_account_movements_and_balance(self, books):
        client = api_for(books.owner)
        made = client.post(
            reverse("ledger-cash-account-list"),
            {"kind": "bank", "name": "Ziraat", "iban": "TR120001000000000000000001"},
            format="json",
        )
        assert made.status_code == 201, made.data
        assert made.data["balance"] == "0.00"
        bank = CashAccount.objects.get(pk=made.data["id"])

        receive(books, "250.00", cash_account=bank)
        record_payment_out(
            account=supplier_account(books),
            cash_account=bank,
            amount=Decimal("100.00"),
            method=PaymentMethod.TRANSFER,
            date=local_today(),
            note="",
            by=books.owner,
        )

        detail = client.get(reverse("ledger-cash-account-detail", args=[bank.pk])).data
        assert detail["balance"] == "150.00"
        listed = {
            row["name"]: row["balance"]
            for row in client.get(reverse("ledger-cash-account-list")).data["results"]
        }
        assert listed == {"Merkez kasa": "0.00", "Ziraat": "150.00"}

        movements = client.get(reverse("ledger-cash-account-movements", args=[bank.pk])).data
        assert [
            (row["source"], row["cash_direction"], row["amount"]) for row in movements["results"]
        ] == [
            (MovementSource.PAYMENT_OUT, CashDirection.OUT, "100.00"),
            (MovementSource.COLLECTION, CashDirection.IN, "250.00"),
        ]

        url = reverse("ledger-cash-account-detail", args=[bank.pk])
        refused = client.patch(url, {"currency": "EUR"}, format="json")
        assert refused.status_code == 400
        assert error_of(refused)[1] == [("currency", "invalid")]
        renamed = client.patch(url, {"name": "Ziraat TL"}, format="json")
        assert (renamed.status_code, renamed.data["name"], renamed.data["balance"]) == (
            200,
            "Ziraat TL",
            "150.00",
        )

    def test_finance_summary(self, books):
        today = date(2026, 10, 15)
        with (
            patch("apps.ledger.posting.local_today", return_value=today),
            patch("apps.ledger.api.v1.views.local_today", return_value=today),
        ):
            bank = bank_of(books, "Ziraat")
            owed(books, books.customer, "1000.00", days_ago=20)
            parts = supplier_account(books)
            record_manual_entry(
                account=parts,
                direction=Direction.CREDIT,
                amount=Decimal("700.00"),
                date=date(2026, 10, 2),
                reason="Parts bought",
                by=books.owner,
            )
            receive(books, "300.00", date=today)
            receive(books, "200.00", date=date(2026, 10, 3), cash_account=bank)
            record_payment_out(
                account=parts,
                cash_account=books.till,
                amount=Decimal("50.00"),
                method=PaymentMethod.CASH,
                date=date(2026, 10, 14),
                note="",
                by=books.owner,
            )
            # Moving the till to the bank is neither in nor out of the firm.
            record_transfer(
                kind=TransferKind.CASH,
                from_cash=books.till,
                to_cash=bank,
                amount=Decimal("100.00"),
                date=today,
                note="",
                by=books.owner,
            )
            client = api_for(books.owner)
            url = reverse("ledger-finance-summary")
            by_period = {
                p: client.get(url, {"period": p}).data for p in ("today", "yesterday", "month")
            }

        today_view = by_period["today"]
        assert (today_view["date_from"], today_view["date_to"]) == ("2026-10-15", "2026-10-15")
        (lira,) = today_view["currencies"]
        assert lira["currency"] == "TRY"
        # 1000 - 300 - 200 owed by the customer; 700 - 50 owed to the supplier.
        assert (lira["receivables"], lira["payables"]) == ("500.00", "650.00")
        cash = {row["name"]: row for row in lira["cash_accounts"]}
        assert (cash["Merkez kasa"]["balance"], cash["Ziraat"]["balance"]) == ("150.00", "300.00")
        assert (lira["money_in"], lira["money_out"]) == ("300.00", "0.00")
        assert (cash["Merkez kasa"]["money_in"], cash["Ziraat"]["money_in"]) == ("300.00", "0.00")

        (yesterday,) = by_period["yesterday"]["currencies"]
        assert (yesterday["money_in"], yesterday["money_out"]) == ("0.00", "50.00")
        month = by_period["month"]
        (month_lira,) = month["currencies"]
        assert (month["date_from"], month_lira["money_in"], month_lira["money_out"]) == (
            "2026-10-01",
            "500.00",
            "50.00",
        )
        assert api_for(books.owner).get(url, {"period": "decade"}).status_code == 400

    def test_operations_records_a_collection_from_what_it_can_see(self, books):
        ops = api_for(staff(books.company, Role.OPERATIONS))
        draft = owed(books, books.customer, "800.00", days_ago=10)

        # Only ids its own reads hand it: the customer page and the till picker.
        customer = ops.get(reverse("customer-detail", args=[books.customer.pk])).json()
        assert customer["accounts"] == [
            {"id": str(books.account.pk), "balance": "800.00", "currency": "TRY"}
        ]
        tills = ops.get(reverse("ledger-cash-account-list")).json()["results"]
        till = next(row for row in tills if row["name"] == "Merkez kasa")
        drafts = ops.get(reverse("invoice-draft-list"))
        if drafts.status_code == 200:
            invoice_id = drafts.json()["results"][0]["id"]
        else:
            invoice_id = None

        body: dict[str, Any] = {
            "account": customer["accounts"][0]["id"],
            "cash_account": till["id"],
            "amount": "300.00",
            "method": "cash",
        }
        if invoice_id is not None:
            body["applies"] = [{"invoice": invoice_id, "amount": "300.00"}]
        response = keyed(ops, reverse("ledger-collection-list"), body)

        assert response.status_code == 201, response.data
        after = ops.get(reverse("customer-detail", args=[books.customer.pk])).json()
        assert after["accounts"][0]["balance"] == "500.00"
        if invoice_id is not None:
            assert invoice_remaining(draft) == Decimal("500.00")

    def test_the_picker_shows_no_money(self, books):
        bank = CashAccount.objects.create(
            company=books.company, kind=CashKind.BANK, name="Ziraat", iban="TR1200010000000001"
        )
        receive(books, "900.00", cash_account=bank)
        receive(books, "10.00")
        parts = supplier_account(books)
        record_payment_out(
            account=parts,
            cash_account=bank,
            amount=Decimal("50.00"),
            method=PaymentMethod.TRANSFER,
            date=local_today(),
            note="",
            by=books.owner,
        )
        money = {"balance", "iban"}
        ops = api_for(staff(books.company, Role.OPERATIONS))
        cash_list = reverse("ledger-cash-account-list")
        cash_detail = reverse("ledger-cash-account-detail", args=[bank.pk])

        rows = ops.get(cash_list).json()["results"]
        assert {row["name"] for row in rows} == {"Merkez kasa", "Ziraat"}
        for row in rows:
            assert set(row) == {"id", "name", "kind", "currency", "is_active"}
        assert not money & set(ops.get(cash_detail).json())

        # Ordering by a balance it may not see tells it nothing: ignored.
        for ordering in ("-balance", "balance"):
            response = ops.get(cash_list, {"ordering": ordering})
            assert response.status_code == 200, response.data
            assert [row["name"] for row in response.json()["results"]] == [
                "Merkez kasa",
                "Ziraat",
            ]

        suppliers = ops.get(reverse("ledger-supplier-list")).json()["results"]
        assert suppliers
        for row in suppliers:
            assert "balance" not in row
        supplier = ops.get(reverse("ledger-supplier-detail", args=[suppliers[0]["id"]])).json()
        assert "balance" not in supplier

        # Whoever reads the ledger still gets the full shape.
        accountant = api_for(staff(books.company, Role.ACCOUNTANT))
        full = accountant.get(cash_list, {"ordering": "-balance"}).json()["results"]
        assert [(row["name"], row["balance"]) for row in full] == [
            ("Ziraat", "850.00"),
            ("Merkez kasa", "10.00"),
        ]
        assert accountant.get(cash_detail).json()["iban"] == "TR1200010000000001"

    def test_the_customer_read_shows_the_account_to_who_may_see_it(
        self, books, django_assert_max_num_queries
    ):
        owed(books, books.customer, "100.00", days_ago=1)
        tech = staff(books.company, Role.TECHNICIAN)
        UserCustomer.objects.create(
            company=books.company, user=tech, customer=books.customer, assigned_by=books.owner
        )
        url = reverse("customer-detail", args=[books.customer.pk])

        assert "accounts" not in api_for(tech).get(url).json()
        for role in (Role.OPERATIONS, Role.ACCOUNTANT, Role.ADMIN):
            (account,) = api_for(staff(books.company, role)).get(url).json()["accounts"]
            assert (account["id"], account["balance"]) == (str(books.account.pk), "100.00")

        # One query for every row's account, not one per customer.
        client = api_for(books.owner)
        client.get(reverse("customer-list"))
        with django_assert_max_num_queries(12) as few:
            client.get(reverse("customer-list"))
        for n in range(5):
            owed(books, make_customer(books.company, f"More {n}"), "10.00", days_ago=1)
        with django_assert_max_num_queries(len(few.captured_queries)):
            rows = client.get(reverse("customer-list")).json()["results"]
        assert {row["accounts"][0]["balance"] for row in rows} == {"100.00", "10.00"}

    def test_inactive_accounts_take_no_documents(self, books):
        books.account.is_active = False
        books.account.save(update_fields=["is_active", "updated_at"])

        response = keyed(
            api_for(books.owner), reverse("ledger-collection-list"), _collection_body(books)
        )

        assert response.status_code == 400
        assert error_of(response)[1] == [("account", "does_not_exist")]
        assert Collection.objects.count() == 0

    def test_open_account_race_is_a_unique_error(self, books):
        tech = staff(books.company, Role.TECHNICIAN)
        ledger_services.open_account(
            company_id=books.company.id, kind=AccountKind.STAFF, user=tech, by=books.owner
        )
        # The other request passed the pre-check before this one committed.
        with (
            patch.object(ledger_services, "_has_account", return_value=False),
            pytest.raises(ValidationError) as error,
        ):
            ledger_services.open_account(
                company_id=books.company.id, kind=AccountKind.STAFF, user=tech, by=books.owner
            )
        assert codes(error) == {"user": "unique"}
        assert Account.objects.filter(user=tech).count() == 1

    def test_the_books_can_be_closed_and_reopened(self, books):
        owner = api_for(books.owner)
        bookkeeper = staff(books.company, Role.ACCOUNTANT)
        accountant = api_for(bookkeeper)
        url = reverse("ledger-settings")
        closed = local_today() - timedelta(days=10)

        assert owner.get(url).json() == {"closed_through": None}
        future = owner.patch(url, {"closed_through": str(local_today() + timedelta(days=1))})
        assert future.status_code == 400
        assert error_of(future)[1] == [("closed_through", "invalid")]

        response = accountant.patch(url, {"closed_through": str(closed)}, format="json")
        assert response.status_code == 200, response.data
        assert response.json() == {"closed_through": str(closed)}
        # Who closed the books, and to what, is on the audit trail.
        (entry,) = [
            row
            for row in AuditLog.objects.filter(table_name="company", record_id=books.company.pk)
            if (row.new_values or {}).get("ledger_closed_through") == str(closed)
        ]
        assert entry.user_id == bookkeeper.pk

        for day in (closed, closed - timedelta(days=3)):
            refused = keyed(
                owner,
                reverse("ledger-collection-list"),
                {**_collection_body(books), "date": str(day)},
            )
            assert refused.status_code == 400
            assert error_of(refused)[1] == [("date", "PERIOD_CLOSED")]
        allowed = keyed(
            owner,
            reverse("ledger-collection-list"),
            {**_collection_body(books), "date": str(closed + timedelta(days=1))},
        )
        assert allowed.status_code == 201, allowed.data

        # Reopening is allowed: moving the date back, or clearing it.
        earlier = closed - timedelta(days=30)
        assert owner.patch(url, {"closed_through": str(earlier)}, format="json").status_code == 200
        assert (
            keyed(
                owner,
                reverse("ledger-collection-list"),
                {**_collection_body(books), "date": str(closed)},
            ).status_code
            == 201
        )
        assert owner.patch(url, {"closed_through": None}, format="json").json() == {
            "closed_through": None
        }

    def test_the_summary_shows_an_inactive_bank_that_holds_money(self, books):
        bank = bank_of(books, "Old bank")
        receive(books, "120.00", cash_account=bank)
        bank.is_active = False
        bank.save(update_fields=["is_active", "updated_at"])
        empty = bank_of(books, "Closed and empty")
        empty.is_active = False
        empty.save(update_fields=["is_active", "updated_at"])

        view = api_for(books.owner).get(reverse("ledger-finance-summary")).json()

        (lira,) = view["currencies"]
        lines = {row["name"]: row for row in lira["cash_accounts"]}
        assert "Closed and empty" not in lines
        assert (lines["Old bank"]["is_active"], lines["Old bank"]["balance"]) == (False, "120.00")
        assert lines["Old bank"]["money_in"] == "120.00"
        # The totals are the lines, added up.
        assert lira["money_in"] == f"{sum(Decimal(r['money_in']) for r in lines.values()):.2f}"
        assert lira["money_out"] == f"{sum(Decimal(r['money_out']) for r in lines.values()):.2f}"

    def test_a_collection_cancelled_the_same_day_nets_to_nothing(self, books):
        cancel_collection(receive(books, "75.00"), by=books.owner)
        kept = receive(books, "20.00")
        assert kept

        view = api_for(books.owner).get(reverse("ledger-finance-summary")).json()

        (lira,) = view["currencies"]
        assert (lira["money_in"], lira["money_out"]) == ("20.00", "0.00")
        (till,) = lira["cash_accounts"]
        assert (till["money_in"], till["money_out"], till["balance"]) == ("20.00", "0.00", "20.00")

    def test_the_summary_keeps_currencies_apart(self, books):
        dollars = CashAccount.objects.create(
            company=books.company, kind=CashKind.BANK, name="USD", currency="USD"
        )
        euro_customer = make_customer(books.company, "Abroad")
        abroad = Account.objects.get(customer=euro_customer)
        abroad.currency = "USD"
        abroad.save(update_fields=["currency", "updated_at"])
        receive(books, "100.00")
        receive(books, "40.00", account=abroad, cash_account=dollars)

        view = api_for(books.owner).get(reverse("ledger-finance-summary")).json()

        by_currency = {row["currency"]: row for row in view["currencies"]}
        assert set(by_currency) == {"TRY", "USD"}
        assert (by_currency["TRY"]["money_in"], by_currency["TRY"]["payables"]) == (
            "100.00",
            "100.00",
        )
        assert (by_currency["USD"]["money_in"], by_currency["USD"]["payables"]) == (
            "40.00",
            "40.00",
        )
        assert [row["name"] for row in by_currency["USD"]["cash_accounts"]] == ["USD"]

    @pytest.mark.parametrize(
        ("basename", "field"),
        [
            ("ledger-collection", "account"),
            ("ledger-collection", "cash_account"),
            ("ledger-collection", "applies.0.invoice"),
            ("ledger-payment-out", "account"),
            ("ledger-payment-out", "cash_account"),
            ("ledger-manual-entry", "account"),
            ("ledger-transfer", "to_account"),
            ("ledger-transfer", "from_account"),
            ("ledger-transfer", "to_cash"),
            ("ledger-opening", "account"),
            ("ledger-opening", "cash_account"),
            ("ledger-account", "customer"),
            ("ledger-account", "supplier"),
            ("ledger-account", "user"),
        ],
    )
    def test_another_firms_records_are_refused(self, books, basename, field):
        other, other_owner = make_firm("B")
        theirs_customer = make_customer(other, "Theirs")
        with company_context(other.id):
            theirs = SimpleNamespace(
                company=other,
                owner=other_owner,
                customer=theirs_customer,
                account=Account.objects.get(customer=theirs_customer),
                till=CashAccount.objects.get(),
            )
            their_supplier = supplier_account(theirs).supplier
            assert their_supplier is not None
            their_invoice = owed(theirs, theirs_customer, "100.00", days_ago=1)
            their_ids = {
                "account": theirs.account.pk,
                "from_account": theirs.account.pk,
                "to_account": theirs.account.pk,
                "cash_account": theirs.till.pk,
                "to_cash": theirs.till.pk,
                "customer": theirs_customer.pk,
                "supplier": their_supplier.pk,
                "user": other_owner.pk,
            }
        mine_bank = bank_of(books)
        mine_staff = staff(books.company, Role.TECHNICIAN)
        bodies: dict[tuple[str, str], dict[str, Any]] = {
            ("ledger-collection", "applies.0.invoice"): {
                **_collection_body(books),
                "applies": [{"invoice": str(their_invoice.pk), "amount": "10.00"}],
            },
            ("ledger-transfer", "to_account"): {
                "kind": "account",
                "from_account": str(books.account.pk),
                "to_account": str(their_ids["to_account"]),
                "amount": "10.00",
            },
            ("ledger-transfer", "from_account"): {
                "kind": "account",
                "from_account": str(their_ids["from_account"]),
                "to_account": str(books.account.pk),
                "amount": "10.00",
            },
            ("ledger-transfer", "to_cash"): {
                "kind": "cash",
                "from_cash": str(books.till.pk),
                "to_cash": str(their_ids["to_cash"]),
                "amount": "10.00",
            },
            ("ledger-opening", "cash_account"): {
                "cash_account": str(their_ids["cash_account"]),
                "direction": "debit",
                "amount": "10.00",
            },
            ("ledger-account", "customer"): {
                "kind": "customer",
                "customer": str(their_ids["customer"]),
            },
            ("ledger-account", "supplier"): {
                "kind": "supplier",
                "supplier": str(their_ids["supplier"]),
            },
            ("ledger-account", "user"): {"kind": "staff", "user": str(their_ids["user"])},
        }
        if (basename, field) in bodies:
            body = bodies[(basename, field)]
        else:
            body = {**DOCUMENTS[basename](books), field: str(their_ids[field])}
        assert mine_staff  # a staff member of ours exists, so `user` is not refused for want of one
        assert mine_bank
        counts = (
            Movement.unscoped.count(),
            Collection.unscoped.count(),
            Account.unscoped.count(),
            OpeningBalance.unscoped.count(),
            Transfer.unscoped.count(),
        )

        response = keyed(api_for(books.owner), reverse(f"{basename}-list"), body)

        assert response.status_code == 400, response.data
        assert error_of(response) == ("VALIDATION_ERROR", [(field, "does_not_exist")])
        assert (
            Movement.unscoped.count(),
            Collection.unscoped.count(),
            Account.unscoped.count(),
            OpeningBalance.unscoped.count(),
            Transfer.unscoped.count(),
        ) == counts


# The statement ---------------------------------------------------------------------


def contact(books: SimpleNamespace, email: str, *, ticked: bool = True) -> CustomerContact:
    return CustomerContact.objects.create(
        company=books.company,
        customer=books.customer,
        full_name="Contact",
        email=email,
        receives_monthly_report=ticked,
    )


@pytest.fixture
def statement_books(books):
    """A fee before the range, a cancelled collection and a fee inside it.

    Opening at `start` is the 500 fee; then the collection (-100), a 25 fee,
    and the collection's reversal today (+100), which nets the collection out.
    """
    today = local_today()
    books.start = today - timedelta(days=6)
    record_manual_entry(
        account=books.account,
        direction=Direction.DEBIT,
        amount=Decimal("500.00"),
        date=today - timedelta(days=10),
        reason="Old fee",
        by=books.owner,
    )
    books.collection = receive(books, "100.00", date=today - timedelta(days=5))
    record_manual_entry(
        account=books.account,
        direction=Direction.DEBIT,
        amount=Decimal("25.00"),
        date=today - timedelta(days=2),
        reason="Late fee",
        by=books.owner,
    )
    cancel_collection(books.collection, by=books.owner)
    return books


class TestStatement:
    def test_running_balance_and_opening_at_from(self, statement_books):
        books = statement_books
        found = statement_rows(books.account, start=books.start, end=None)
        assert found.opening == Decimal("500.00")
        assert [(r.debit, r.credit, r.balance) for r in found.rows] == [
            (Decimal("0.00"), Decimal("100.00"), Decimal("400.00")),
            (Decimal("25.00"), Decimal("0.00"), Decimal("425.00")),
            (Decimal("100.00"), Decimal("0.00"), Decimal("525.00")),
        ]
        assert found.closing == Decimal("525.00")
        # The cancelled collection is two lines, both naming it, that net out.
        collection_rows = [r for r in found.rows if r.document_id == books.collection.pk]
        assert [r.is_reversal for r in collection_rows] == [False, True]
        assert {r.document_type for r in collection_rows} == {"ledger.collection"}
        assert sum(r.debit - r.credit for r in collection_rows) == Decimal("0.00")

        # Unbounded: the opening is nothing, and the closing is the balance.
        everything = statement_rows(books.account, start=None, end=None)
        assert everything.opening == Decimal("0.00")
        assert len(everything.rows) == 4
        assert everything.closing == account_balance(books.account) == Decimal("525.00")

        # `end` is inclusive, and the closing is the balance through it.
        through = statement_rows(
            books.account, start=books.start, end=local_today() - timedelta(days=5)
        )
        assert [r.credit for r in through.rows] == [Decimal("100.00")]
        assert through.closing == Decimal("400.00")

    def test_the_endpoint_answers_the_same_and_checks_the_range(self, statement_books):
        books = statement_books
        client = api_for(books.owner)
        url = reverse("ledger-account-statement", args=[books.account.pk])
        body = client.get(url, {"start": books.start.isoformat()}).data
        assert (body["opening"], body["closing"]) == ("500.00", "525.00")
        assert (body["total_debit"], body["total_credit"]) == ("125.00", "100.00")
        assert [row["balance"] for row in body["rows"]] == ["400.00", "425.00", "525.00"]
        reversal = body["rows"][-1]
        assert reversal["is_reversal"] is True
        assert reversal["document_type"] == "ledger.collection"
        assert reversal["document_id"] == str(books.collection.pk)

        wrong = client.get(url, {"start": "2026-10-10", "end": "2026-10-01"})
        assert wrong.status_code == 400
        assert error_of(wrong) == (
            "VALIDATION_ERROR",
            [("start", "END_DATE_BEFORE_START_DATE")],
        )

        # Operations records collections but reads no statement, nor sends one.
        operations = api_for(staff(books.company, Role.OPERATIONS))
        for name in ("statement", "statement-csv", "statement-pdf"):
            response = operations.get(reverse(f"ledger-account-{name}", args=[books.account.pk]))
            assert response.status_code == 403, name
        send = reverse("ledger-account-statement-send", args=[books.account.pk])
        assert keyed(operations, send, {}).status_code == 403
        accountant = api_for(staff(books.company, Role.ACCOUNTANT))
        assert accountant.get(url).status_code == 200

    def test_csv_matches_json(self, statement_books):
        books = statement_books
        client = api_for(books.owner)
        params = {"start": books.start.isoformat()}
        body = client.get(reverse("ledger-account-statement", args=[books.account.pk]), params).data
        response = client.get(
            reverse("ledger-account-statement-csv", args=[books.account.pk]), params
        )
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert "attachment" in response["Content-Disposition"]
        text = response.content.decode("utf-8")
        assert text.startswith("\ufeff")
        lines = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
        header, opening, *rows, closing = lines
        assert header == ["Tarih", "Açıklama", "Borç", "Alacak", "Bakiye"]

        def comma(value: str) -> str:
            return value.replace(".", ",")

        assert opening[0] == books.start.strftime("%d.%m.%Y")
        assert opening[-1] == comma(body["opening"]) == "500,00"
        assert len(rows) == len(body["rows"])
        for csv_row, row in zip(rows, body["rows"], strict=True):
            day = date.fromisoformat(row["date"]).strftime("%d.%m.%Y")
            assert csv_row[0] == day
            assert csv_row[1].endswith(row["description"])
            assert csv_row[2:] == [comma(row["debit"]), comma(row["credit"]), comma(row["balance"])]
        assert rows[-1][1].startswith("İptal: ")
        assert closing[2:] == [
            comma(body["total_debit"]),
            comma(body["total_credit"]),
            comma(body["closing"]),
        ]

    def test_html_escapes_and_lists_every_row(self, statement_books):
        books = statement_books
        record_manual_entry(
            account=books.account,
            direction=Direction.CREDIT,
            amount=Decimal("1234.50"),
            date=local_today(),
            reason='<script>alert("x")</script> & co',
            by=books.owner,
        )
        found = statement_rows(books.account, start=None, end=None)
        html = build_statement_html(found, company=books.company)
        assert "<script>" not in html
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in html
        for description in ("Old fee", "Late fee", f"Tahsilat {books.collection.number}"):
            assert description in html
        # The opening, every row, and the closing.
        assert html.count('<td class="date">') == 1 + len(found.rows) + 1
        assert html.count("İptal: ") == 1
        assert "1.234,50" in html
        assert books.company.display_name in html

    def test_pdf_renders(self, statement_books):
        books = statement_books
        response = api_for(books.owner).get(
            reverse("ledger-account-statement-pdf", args=[books.account.pk])
        )
        try:
            import weasyprint  # noqa: F401
        except (ImportError, OSError):
            assert response.status_code == 503
            pytest.skip("WeasyPrint or Pango is not available here")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
        found = statement_rows(books.account, start=None, end=None)
        assert render_statement_pdf(found, company=books.company).startswith(b"%PDF")

    def test_send_goes_to_report_recipients_with_the_pdf(
        self, statement_books, monkeypatch, django_capture_on_commit_callbacks
    ):
        books = statement_books
        monkeypatch.setattr(
            "apps.ledger.statement.render_statement_pdf", lambda statement, *, company: b"%PDF-1"
        )
        contact(books, "one@example.com")
        contact(books, "two@example.com")
        contact(books, "", ticked=True)
        contact(books, "not-ticked@example.com", ticked=False)
        client = api_for(books.owner)
        url = reverse("ledger-account-statement-send", args=[books.account.pk])
        body = {"start": books.start.isoformat()}

        assert client.post(url, body, format="json").status_code == 400  # no key

        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            response = keyed(client, url, body, key="send-1")
        assert response.status_code == 200, response.data
        assert response.data == {"recipients": ["one@example.com", "two@example.com"]}
        # Nothing went while the transaction was open: one send per recipient waits.
        assert django_mail.outbox == []
        assert len(callbacks) == 2
        for callback in callbacks:
            callback()
        assert sorted(m.to[0] for m in django_mail.outbox) == [
            "one@example.com",
            "two@example.com",
        ]
        for message in django_mail.outbox:
            assert books.account.name in message.subject
            assert "525,00" in message.body
            ((filename, content, mime_type),) = message.attachments
            assert (content, mime_type) == (b"%PDF-1", "application/pdf")
            assert filename == f"ekstre-{books.start.isoformat()}.pdf"

        # A retry with the same key answers the first send and mails nobody.
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            again = keyed(client, url, body, key="send-1")
        assert again.status_code == 200
        assert django_mail.outbox == []

    def test_csv_cells_never_run_as_formulas(self, statement_books):
        books = statement_books
        record_manual_entry(
            account=books.account,
            direction=Direction.CREDIT,
            amount=Decimal("1000.00"),
            date=local_today(),
            reason='=HYPERLINK("http://x")',
            by=books.owner,
        )
        found = statement_rows(books.account, start=None, end=None)
        text = statement_csv(found)
        *_, formula, closing = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
        assert formula[1] == '\'=HYPERLINK("http://x")'
        # Numbers the code formats itself stay numbers, minus sign and all.
        assert formula[2:] == ["0,00", "1000,00", "-475,00"]
        assert closing[-1] == "-475,00"

    def test_rows_on_the_same_day_keep_the_order_they_were_made_in(self, books):
        day = local_today() - timedelta(days=1)
        for amount, reason in (("10.00", "First"), ("3.00", "Second"), ("7.00", "Third")):
            record_manual_entry(
                account=books.account,
                direction=Direction.DEBIT,
                amount=Decimal(amount),
                date=day,
                reason=reason,
                by=books.owner,
            )
        rows = statement_rows(books.account, start=day, end=day).rows
        assert [(r.description, r.balance) for r in rows] == [
            ("First", Decimal("10.00")),
            ("Second", Decimal("13.00")),
            ("Third", Decimal("20.00")),
        ]
        # Made in the same instant, the id breaks the tie (ids are time-ordered).
        Movement.objects.filter(account=books.account).update(created_at=timezone.now())
        ids = sorted(Movement.objects.filter(account=books.account).values_list("pk", flat=True))
        rows = statement_rows(books.account, start=day, end=day).rows
        assert [r.movement_id for r in rows] == ids
        assert rows[-1].balance == Decimal("20.00")

    def test_a_subject_reads_as_written_and_the_mail_is_sent(
        self, statement_books, monkeypatch, django_capture_on_commit_callbacks
    ):
        books = statement_books
        monkeypatch.setattr(
            "apps.ledger.statement.render_statement_pdf", lambda statement, *, company: b"%PDF-1"
        )
        contact(books, "one@example.com")
        Account.objects.filter(pk=books.account.pk).update(name="Yılmaz & Oğlu\nX")
        account = Account.objects.get(pk=books.account.pk)
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            send_statement(account, start=None, end=None, by=books.owner)
        (message,) = django_mail.outbox
        assert message.subject == "Firm A — hesap ekstresi (Yılmaz & Oğlu X)"

    def test_send_without_a_recipient_is_refused(self, books, django_capture_on_commit_callbacks):
        contact(books, "not-ticked@example.com", ticked=False)
        url = reverse("ledger-account-statement-send", args=[books.account.pk])
        with django_capture_on_commit_callbacks(execute=True):
            response = keyed(api_for(books.owner), url, {})
        assert response.status_code == 409
        assert error_of(response)[0] == "REPORT_NO_RECIPIENT"
        assert django_mail.outbox == []
        # A supplier's account has no report recipients at all.
        supplier = supplier_account(books)
        with pytest.raises(Conflict):
            send_statement(supplier, start=None, end=None, by=books.owner)


# Concurrency, on PostgreSQL ------------------------------------------------------
#
# SQLite has no row locks (it ignores FOR UPDATE and serialises whole
# transactions), so these only mean something on PostgreSQL, where CI runs
# them. Real threads, each on its own connection, released together by a
# barrier; the services are called directly, as a view would.

_JOIN_TIMEOUT = 30


def _race(*calls: Callable[[], Any]) -> list[Any]:
    """Run each call in its own thread at once; each result or the exception raised."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(index: int, call: Callable[[], Any]) -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=_JOIN_TIMEOUT)
            results[index] = call()
        except BaseException as exc:  # reported to the test thread
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "a thread is stuck: a deadlock?"
    return results


@unittest.skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL")
class TestConcurrency(TransactionTestCase):
    def setUp(self) -> None:
        self.company, self.owner = make_firm("C")
        self.customer = make_customer(self.company)
        with company_context(self.company.id):
            self.books = SimpleNamespace(
                company=self.company,
                owner=self.owner,
                customer=self.customer,
                account=Account.objects.get(customer=self.customer),
                till=CashAccount.objects.get(),
            )
            self.invoice = owed(self.books, self.customer, "1000.00", days_ago=3)

    def _in_firm(self, call: Callable[[], Any]) -> Callable[[], Any]:
        def wrapped() -> Any:
            with company_context(self.company.id):
                return call()

        return wrapped

    def _collect(self, amount: str) -> Callable[[], Any]:
        def call() -> Collection:
            invoice = InvoiceDraft.objects.get(pk=self.invoice.pk)
            return receive(self.books, amount, applies=[(invoice, Decimal(amount))])

        return self._in_firm(call)

    def test_two_full_collections_on_one_invoice_one_wins(self) -> None:
        results = _race(self._collect("1000.00"), self._collect("1000.00"))

        won = [r for r in results if isinstance(r, Collection)]
        lost = [r for r in results if isinstance(r, ValidationError)]
        assert (len(won), len(lost)) == (1, 1), results
        assert lost[0].get_codes() == {"applies": "OVERPAYMENT_ON_INVOICE"}
        with company_context(self.company.id):
            assert invoice_remaining(InvoiceDraft.objects.get(pk=self.invoice.pk)) == Decimal(
                "0.00"
            )
            assert Collection.objects.count() == 1
            assert account_balance(self.books.account) == Decimal("0.00")

    def test_a_cancel_racing_a_collection_neither_deadlocks_nor_loses_money(self) -> None:
        with company_context(self.company.id):
            standing = receive(self.books, "400.00", applies=[(self.invoice, Decimal("400.00"))])

        results = _race(
            self._in_firm(lambda: cancel_collection(standing, by=self.owner)),
            self._collect("400.00"),
        )

        assert not [r for r in results if isinstance(r, BaseException)], results
        with company_context(self.company.id):
            movements = list(Movement.objects.all())
            debits = sum(
                (m.amount for m in movements if m.direction == Direction.DEBIT), Decimal(0)
            )
            credits = sum(
                (m.amount for m in movements if m.direction == Direction.CREDIT), Decimal(0)
            )
            cash_in = sum(
                (m.amount for m in movements if m.cash_direction == CashDirection.IN), Decimal(0)
            )
            cash_out = sum(
                (m.amount for m in movements if m.cash_direction == CashDirection.OUT), Decimal(0)
            )
            assert account_balance(self.books.account) == debits - credits == Decimal("600.00")
            assert cash_balance(self.books.till) == cash_in - cash_out == Decimal("400.00")
            invoice = InvoiceDraft.objects.get(pk=self.invoice.pk)
            assert invoice_remaining(invoice) == Decimal("600.00")
            assert invoice.status == InvoiceStatus.ISSUED

    def test_a_concurrent_retried_collection_posts_once(self) -> None:
        # A device that retries before its first request has answered: the
        # same key, the same body, both in flight together. The key is
        # reserved before the work, so the second either finds it in flight
        # (409) or, if the first has already committed, gets its answer
        # replayed (201, the same body). Either way one collection.
        url = reverse("ledger-collection-list")
        body = {
            "account": str(self.books.account.pk),
            "cash_account": str(self.books.till.pk),
            "amount": "100.00",
            "method": "cash",
        }
        clients = [api_for(self.owner), api_for(self.owner)]

        def send(client: APIClient) -> Callable[[], Any]:
            return lambda: keyed(client, url, body, key="race-retry-1")

        results = _race(*(send(client) for client in clients))

        assert not [r for r in results if isinstance(r, BaseException)], results
        statuses = sorted(r.status_code for r in results)
        assert statuses in ([201, 409], [201, 201]), [r.data for r in results]
        if statuses == [201, 409]:
            refused = next(r for r in results if r.status_code == 409)
            assert error_of(refused)[0] == "IDEMPOTENCY_KEY_IN_FLIGHT"
        else:
            assert results[0].data == results[1].data
        with company_context(self.company.id):
            assert Collection.objects.count() == 1
            assert Movement.objects.filter(source=MovementSource.COLLECTION).count() == 1
            assert account_balance(self.books.account) == Decimal("900.00")


# Final fix pass ---------------------------------------------------------------------
#
# The rulings of the final review (.superpowers/sdd/2026-10-23-f0-ledger-api,
# "Final:"): C1 accounts per currency, I1 the carry-over job, I2 settling an
# advance, and the minors re-graded into the pass (M1, M3, M4, M5).


def euro_draft(
    books: SimpleNamespace, number: str = "EUR-1", price: str = "100.00"
) -> InvoiceDraft:
    """A EUR draft with one priced line, issued through M7: `price` + 20% VAT."""
    draft = make_invoice(books, status=InvoiceStatus.DRAFT, currency="EUR")
    billing.add_line(
        draft,
        description="Maintenance",
        quantity=Decimal("1"),
        unit_price=Decimal(price),
        by=books.owner,
    )
    return billing.issue(
        draft,
        invoice_number=number,
        invoice_date=local_today() - timedelta(days=5),
        by=books.owner,
    )


def euro_till(books: SimpleNamespace, name: str = "EUR bank") -> CashAccount:
    return CashAccount.objects.create(
        company=books.company, kind=CashKind.BANK, name=name, currency="EUR"
    )


def account_in(customer: Customer, currency: str) -> Account:
    return Account.objects.get(customer=customer, currency=currency)


class TestAccountsPerCurrency:
    """C1: a customer has one account per currency, and a non-TRY invoice posts
    to the account in its own currency."""

    def test_a_firm_with_euro_invoices_migrates_cleanly(self, books):
        issued = make_invoice(books, "120.00", currency="EUR")
        paid = make_invoice(books, "60.00", status=InvoiceStatus.PAID, currency="EUR")
        lira = make_invoice(books, "1000.00")

        migration = importlib.import_module("apps.ledger.migrations.0008_carry_over_invoices")
        with system_context():
            migration.carry_over(global_apps, None)
            again = carry_over_invoices()
        assert (again.posted, again.collected) == (0, 0)

        euros = account_in(books.customer, "EUR")
        assert (euros.kind, euros.name) == (AccountKind.CUSTOMER, books.customer.legal_name)
        assert account_balance(euros) == Decimal("120.00")
        assert account_balance(books.account) == Decimal("1000.00")
        (devir,) = CashAccount.objects.filter(role=CashAccountRole.CARRIED_OVER)
        assert (devir.currency, cash_balance(devir)) == ("EUR", Decimal("60.00"))
        assert {m.document_id: m.account_id for m in movements_of(issued)} == {issued.pk: euros.pk}
        assert movements_of(lira)[0].account_id == books.account.pk
        assert (fresh(paid).status, invoice_remaining(fresh(paid))) == (
            InvoiceStatus.PAID,
            Decimal("0.00"),
        )

    def test_each_currency_gets_its_own_carried_over_till(self, books):
        make_invoice(books, "60.00", status=InvoiceStatus.PAID, currency="EUR")
        make_invoice(books, "500.00", status=InvoiceStatus.PAID)

        with system_context():
            carry_over_invoices()

        tills = CashAccount.objects.filter(role=CashAccountRole.CARRIED_OVER)
        assert sorted((t.currency, cash_balance(t)) for t in tills) == [
            ("EUR", Decimal("60.00")),
            ("TRY", Decimal("500.00")),
        ]
        # The default till stays the one TRY till.
        assert list(
            CashAccount.objects.filter(role=CashAccountRole.DEFAULT).values_list("currency")
        ) == [("TRY",)]

    def test_issuing_a_euro_draft_posts_to_the_euro_account(self, books):
        draft = euro_draft(books)

        euros = account_in(books.customer, "EUR")
        assert account_balance(euros) == Decimal("120.00")
        assert account_balance(books.account) == Decimal("0.00")
        (debit,) = movements_of(draft)
        assert (debit.account_id, debit.currency) == (euros.pk, "EUR")

    def test_lira_and_euro_invoices_of_one_customer_go_to_two_accounts(self, books):
        issued_through_m7(books)
        euro_draft(books)

        accounts = Account.objects.filter(customer=books.customer)
        assert sorted((a.currency, account_balance(a)) for a in accounts) == [
            ("EUR", Decimal("120.00")),
            ("TRY", Decimal("1200.00")),
        ]

    def test_a_rename_renames_every_account_of_the_customer(self, books):
        euro_draft(books)

        books.customer.legal_name = "Renamed"
        books.customer.save()

        assert sorted(
            Account.objects.filter(customer=books.customer).values_list("currency", "name")
        ) == [("EUR", "Renamed"), ("TRY", "Renamed")]

    def test_a_staff_account_can_be_opened_in_a_second_currency(self, books):
        clerk = staff(books.company, Role.OPERATIONS, "clerk")
        ledger_services.open_account(
            company_id=books.company.pk, kind=AccountKind.STAFF, user=clerk, by=books.owner
        )

        euros = ledger_services.open_account(
            company_id=books.company.pk,
            kind=AccountKind.STAFF,
            user=clerk,
            currency="EUR",
            by=books.owner,
        )

        assert euros.currency == "EUR"
        with pytest.raises(ValidationError) as error:
            ledger_services.open_account(
                company_id=books.company.pk,
                kind=AccountKind.STAFF,
                user=clerk,
                currency="EUR",
                by=books.owner,
            )
        assert codes(error) == {"user": "unique"}

    def test_euro_balances_and_collections_stay_in_euro(self, books):
        draft = euro_draft(books)
        till = euro_till(books)

        ledger_services.collect_invoice(
            draft, date=local_today(), by=books.owner, cash_account=till
        )

        assert account_balance(account_in(books.customer, "EUR")) == Decimal("0.00")
        assert cash_balance(till) == Decimal("120.00")
        assert cash_balance(books.till) == Decimal("0.00")
        assert fresh(draft).status == InvoiceStatus.PAID

    def test_mark_paid_on_a_euro_invoice_asks_for_a_euro_till(self, books):
        draft = euro_draft(books)
        url = reverse("invoice-draft-mark-paid", args=[draft.pk])
        client = api_for(books.owner)

        refused = client.post(url, {"paid_on": str(local_today())})

        assert refused.status_code == 400, refused.data
        assert error_of(refused)[1] == [("cash_account", "CURRENCY_MISMATCH")]
        assert not Collection.objects.exists()
        # The envelope carries the code; the message, for the log and the
        # service's callers, says what to do.
        with pytest.raises(ValidationError) as error:
            billing.mark_paid(draft, paid_on=local_today(), by=books.owner)
        assert "pick a cash account in EUR" in str(error.value.detail)

        till = euro_till(books)
        paid = client.post(url, {"paid_on": str(local_today()), "cash_account": str(till.pk)})
        assert paid.status_code == 200, paid.data
        assert paid.data["status"] == InvoiceStatus.PAID
        assert cash_balance(till) == Decimal("120.00")

    def test_the_statement_and_the_summary_keep_currencies_apart(self, books):
        issued_through_m7(books)
        euro_draft(books)
        client = api_for(books.owner)

        euros = account_in(books.customer, "EUR")
        rows = client.get(reverse("ledger-account-statement", args=[euros.pk])).json()["rows"]
        assert [(row["debit"], row["credit"]) for row in rows] == [("120.00", "0.00")]
        rows = client.get(reverse("ledger-account-statement", args=[books.account.pk])).json()[
            "rows"
        ]
        assert [(row["debit"], row["credit"]) for row in rows] == [("1200.00", "0.00")]

        view = client.get(reverse("ledger-finance-summary")).json()
        owed_by_currency = {row["currency"]: row["receivables"] for row in view["currencies"]}
        assert owed_by_currency == {"TRY": "1200.00", "EUR": "120.00"}

    def test_overdue_matches_the_invoice_currency(self, books):
        stale = euro_draft(books)
        InvoiceDraft.objects.filter(pk=stale.pk).update(invoice_date=date(2026, 1, 1))
        # The TRY account owes too, but on nothing overdue.
        record_manual_entry(
            account=books.account,
            direction=Direction.DEBIT,
            amount=Decimal("10.00"),
            date=local_today(),
            reason="fee",
            by=books.owner,
        )

        rows = (
            api_for(books.owner)
            .get(reverse("ledger-account-list"), {"overdue": "true"})
            .json()["results"]
        )

        assert [row["id"] for row in rows] == [str(account_in(books.customer, "EUR").pk)]

    def test_the_customer_read_lists_every_account(self, books, django_assert_max_num_queries):
        issued_through_m7(books)
        euro_draft(books)
        url = reverse("customer-detail", args=[books.customer.pk])

        accounts = api_for(books.owner).get(url).json()["accounts"]

        assert sorted((row["currency"], row["balance"]) for row in accounts) == [
            ("EUR", "120.00"),
            ("TRY", "1200.00"),
        ]
        tech = staff(books.company, Role.TECHNICIAN)
        UserCustomer.objects.create(
            company=books.company, user=tech, customer=books.customer, assigned_by=books.owner
        )
        assert "accounts" not in api_for(tech).get(url).json()

        client = api_for(books.owner)
        client.get(reverse("customer-list"))
        with django_assert_max_num_queries(12) as few:
            client.get(reverse("customer-list"))
        for n in range(4):
            euro_draft(books, number=f"EUR-{n + 2}")
            owed(books, make_customer(books.company, f"More {n}"), "10.00", days_ago=1)
        with django_assert_max_num_queries(len(few.captured_queries)):
            rows = client.get(reverse("customer-list")).json()["results"]
        assert all("accounts" in row for row in rows)
