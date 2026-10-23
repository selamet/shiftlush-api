"""F0: the ledger. Accounts, cash accounts and the movements between them.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. A balance is never
stored: it is the sum of the movements, so the tests here create movements
directly and ask the aggregate queries what they add up to.
"""

from __future__ import annotations

import importlib
import random
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.apps import apps as global_apps
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.companies.models import Company
from apps.customers.models import Customer, CustomerType
from apps.ledger.balances import account_balance, cash_balance, with_balance, with_cash_balance
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
    CashDirection,
    CashKind,
    Direction,
    Movement,
    MovementImmutableError,
    MovementSource,
    Supplier,
)
from apps.ledger.posting import check_date, post, reverse_document
from apps.ledger.services import DEFAULT_CASH_ACCOUNT_NAME, account_for_customer
from apps.users.models import User
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
