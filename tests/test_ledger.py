"""F0: the ledger. Accounts, cash accounts and the movements between them.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. A balance is never
stored: it is the sum of the movements, so the tests here create movements
directly and ask the aggregate queries what they add up to.
"""

from __future__ import annotations

import importlib
import random
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.apps import apps as global_apps
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.companies.models import Company
from apps.contracts.models import BillingPeriod, Contract, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.ledger import services as ledger_services
from apps.ledger.balances import account_balance, cash_balance, with_balance, with_cash_balance
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
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
        opening = record_opening(
            account=books.account,
            direction=Direction.DEBIT,
            amount=Decimal("900.00"),
            date=local_today(),
            by=books.owner,
        )
        [movement] = movements_of(opening)
        assert movement.source == "opening"
        assert account_balance(books.account) == Decimal("900.00")

        # One live opening per account.
        with pytest.raises(ValidationError) as error:
            record_opening(
                account=books.account,
                direction=Direction.DEBIT,
                amount=Decimal("1.00"),
                date=local_today(),
                by=books.owner,
            )
        assert codes(error) == {"account": "unique"}

        # Reversed and entered again, before anything else happened.
        cancel_opening(opening, by=books.owner)
        record_opening(
            account=books.account,
            direction=Direction.DEBIT,
            amount=Decimal("950.00"),
            date=local_today(),
            by=books.owner,
        )
        assert account_balance(books.account) == Decimal("950.00")

        # After another movement, an opening is refused, on either side.
        receive(books, "10.00")
        till_opening = {
            "cash_account": books.till,
            "direction": Direction.DEBIT,
            "amount": Decimal("5.00"),
            "date": local_today(),
            "by": books.owner,
        }
        with pytest.raises(ValidationError) as error:
            record_opening(**till_opening)
        assert codes(error) == {"date": "invalid"}
        assert "Opening balance must come first" in str(error.value)
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
