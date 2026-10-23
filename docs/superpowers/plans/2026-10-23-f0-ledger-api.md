# F0 Ledger, API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every party gets an account and every money event gets an
immutable movement. M7 invoices post to it. The office records collections,
payments out, manual entries, transfers and opening balances, reads
statements and a finance summary, and nothing can make a balance disagree
with its movements.

**Architecture:** A new app, `apps.ledger`, with three layers:

1. `models.py`: the parties, the cash accounts, `Movement`, and the
   documents.
2. `posting.py`: the only code that writes movements. It owns currency,
   date, closed-period and reversal rules.
3. `services.py`: creates and cancels documents by calling `posting`.

M7's `apps.billing.services` calls `ledger.services` on issue, cancel and
pay. Views call only `services`. Balances are always computed with
aggregate queries (`balances.py`).

**Tech Stack:** Django 5, DRF, django-filter, drf-spectacular, WeasyPrint
(the statement PDF, as in M7 and the reports), pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-f0-ledger-design.md`

## Global Constraints

- Branch `SL-179`, worktree `~/projects/shiftlush-api-SL-179`. Commits are
  titled `SL-179-<type>: …` in English, with no AI attribution lines.
- There are no Turkish characters in `.py` under `apps/`, `core/` or
  `config/`. Turkish lives in `templates/` and `core/data/` only.
- Money is `Decimal`, `max_digits=14`, `decimal_places=2`, and greater than
  0 on every movement. There is one currency per account and per cash
  account, with no conversion; the default is `TRY`.
- A posted `Movement` is never updated or deleted. Corrections are reversals
  (`source="reversal"`, `reverses` FK unique).
- `(source, document_type, document_id)` is unique for the sources `invoice`
  and `invoice_cancel`.
- New error codes: `CURRENCY_MISMATCH` (400), `PERIOD_CLOSED` (400),
  `ALREADY_REVERSED` (409), `INVOICE_HAS_COLLECTIONS` (409) and
  `OVERPAYMENT_ON_INVOICE` (400).
- Document number prefixes: collections `T-`, payments out `O-`, sequential
  per firm.
- Permissions follow the spec's table:
  - read: owner, admin, accountant (operations sees balances only, through
    the customer read);
  - collections and payments out: owner, admin, accountant, operations;
  - manual entries, transfers, openings and the closed-through date: owner
    and accountant.
- The business date is Istanbul's (`core.scheduling.local_today`). It is
  never after today, and never on or before
  `Company.ledger_closed_through`.
- The `make spec` contract is committed. Done means `make lint`,
  `make mypy`, `make check` and `make test` pass. Every routed model gets
  its record in `tests/test_tenant_isolation.py`.

## Review Focus

1. **A retried collection create** (same `Idempotency-Key`) makes one
   collection and one movement. Pinned by `test_a_retried_collection_posts_once`
   (Task 5).
2. **Two collections applied to the same invoice concurrently** can't
   together exceed its remaining balance. The invoice row is locked
   (`select_for_update`) while applying. Pinned by
   `test_applies_lock_the_invoice` (Task 3), which asserts the lock is taken
   (a query count or `select_for_update` spy).
3. **Cancelling a collection that paid an invoice** sets the invoice back to
   `issued` with the right `remaining`. Pinned by
   `test_cancelling_a_collection_reopens_the_invoice` (Task 4).
4. **Renaming a customer** updates its account's `name`, and the statement
   header shows the current name. Pinned by
   `test_an_account_follows_its_customers_name` (Task 1).
5. **A movement whose currency differs** from its account or its cash
   account is refused before anything is written. Pinned by
   `test_a_currency_mismatch_writes_nothing` (Task 2).

---

### Task 1: The app, the parties, the cash accounts

**Files:**
- Create the `apps/ledger` package: `__init__.py`, `apps.py`, `models.py`,
  `balances.py`, `signals.py`, `migrations/`, and an `api/v1/` skeleton.
- Modify:
  - `config/settings/base.py`: add `"apps.ledger"` after `"apps.reports"`;
  - `apps/companies/models.py`: add
    `ledger_closed_through = DateField(null=True, blank=True)`;
  - `apps/users/services.register_company`: create "Merkez kasa";
  - `core/error_codes.py`: the five codes;
  - `core/permissions.py`: the resources `ledger` (read), `ledger_document`
    (collections and payments out) and `ledger_admin` (manual, transfer,
    opening, closed-through).
- Data migration: an `Account` for every existing customer, and a "Merkez
  kasa" `CashAccount` for every company.
- Test: `tests/test_ledger.py` (new).

**Interfaces:**
- Produces, in `apps.ledger.models`:
  - `AccountKind`: customer, supplier, staff;
  - `CashKind`: cash, bank, card;
  - `Supplier`: `name`, `tax_office`, `tax_number`, `phone`, `email`,
    `address_note`, `notes`;
  - `Account`: `kind`, `customer`, `supplier`, `user` (one of them, by a
    check constraint; each unique), `name`, `currency`, `is_active`;
  - `CashAccount`: `kind`, `name`, `currency`, `iban`, `is_active`;
  - `Movement`: as in the spec, with `MovementSource` choices, `direction`
    (`debit`, `credit`), `cash_direction` (`in`, `out`, or blank),
    `document_type` (`CharField(40)`), `document_id` (`UUIDField` nullable),
    and `reverses` (`OneToOneField` to self, nullable).
- Produces, in `apps.ledger.balances`:
  - `account_balance(account: Account) -> Decimal`;
  - `with_balance(qs: QuerySet[Account]) -> QuerySet[Account]`, which
    annotates `balance`;
  - `cash_balance(cash: CashAccount) -> Decimal`;
  - `with_cash_balance(qs)`;
  - an account's balance is the sum of its debits minus its credits; a cash
    account's is the sum of `in` minus `out`.
- Produces, in `apps.ledger.services`: `account_for_customer(customer: Customer) -> Account`
  (get or create), used by M7 and the signal.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_new_customer_has_an_account`: create a customer through the API,
    and one `Account(kind=customer)` exists with the same name.
  - `test_an_account_follows_its_customers_name`.
  - `test_an_account_names_exactly_one_party`: a check-constraint
    `IntegrityError` for zero parties or two.
  - `test_a_new_company_has_merkez_kasa`: through `register_company`.
  - `test_the_migration_gives_existing_customers_accounts_once`: call the
    data-migration function twice; there is one account per customer.
  - `test_balances_are_sums`: create movements directly (an ORM fixture
    helper). `account_balance`, `with_balance` and `cash_balance` agree.
- [ ] **Step 2: Run them to confirm they fail:**
  `.venv/bin/python -m pytest tests/test_ledger.py -q -p no:cacheprovider`.
- [ ] **Step 3: Implement.** The customer account is created by a
  `post_save` receiver on `Customer` (created, and name changes) in
  `signals.py`, wired in `apps.py.ready`. Rule: it is a signal rather than a
  hook in a service, because customers are created by the viewset, the demo
  and imports alike. Record this in the ledger.
- [ ] **Step 4: Run them to confirm they pass,** plus the customers and
  team suites.
- [ ] **Step 5: Commit:**
  `SL-179-feat: every customer has an account, every firm a till`.

### Task 2: Posting: the only writer of movements

**Files:** Create `apps/ledger/posting.py`. Test in `tests/test_ledger.py`,
class `TestPosting`.

**Interfaces:**
- Produces, in `apps.ledger.posting`:
  - `post(*, account: Account | None, cash_account: CashAccount | None, direction: str | None, cash_direction: str | None, amount: Decimal, currency: str, date: date, source: str, document: Model | None, description: str, by: User | None) -> Movement`;
  - `reverse_document(document: Model, *, date: date, by: User) -> list[Movement]`,
    which posts the mirror of every unreversed movement whose
    `(document_type, document_id)` is this document's, and raises
    `Conflict(ALREADY_REVERSED)` when there's none left;
  - `check_date(company: Company, day: date) -> None`, which raises
    `field_error("date", PERIOD_CLOSED)` or `field_error("date", "invalid")`
    for a future date.
  - `document_type` is `f"{model._meta.app_label}.{model._meta.model_name}"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_post_writes_one_movement_with_both_sides`.
  - `test_a_currency_mismatch_writes_nothing`: the account is TRY, the cash
    account is USD → `CURRENCY_MISMATCH`, and the `Movement` count is
    unchanged.
  - `test_amount_must_be_positive`.
  - `test_dates_after_today_or_in_a_closed_period_are_refused`.
  - `test_reverse_mirrors_and_only_once`: reverse twice → the second raises
    `ALREADY_REVERSED`. The balances return to zero.
  - `test_invariant_after_a_random_sequence`: with
    `random.Random(5)  # noqa: S311`, 200 random posts and reversals across 3
    accounts and 2 cash accounts. Each balance equals a Python-side sum over
    the operations applied.
  - `test_movements_cannot_be_edited`: saving an existing `Movement` raises.
    `Movement.save` refuses updates, and `delete` raises.
- [ ] **Step 2: Run them to confirm they fail. Step 3: Implement. Step 4:
  run them to confirm they pass.**
- [ ] **Step 5: Commit:** `SL-179-feat: one way to write money, and only
  forward`.

### Task 3: The documents

**Files:** Modify `apps/ledger/models.py` (documents and `DocumentCounter`)
and `apps/ledger/services.py`. Test: class `TestDocuments`.

**Interfaces:**
- Produces, in models:
  - `Collection`: `number`, `account`, `cash_account`, `amount`, `method`
    (`cash`, `transfer`, `card`), `date`, `note`, `cancelled_at`;
  - `CollectionApplication`: `collection`, `invoice` (FK
    `billing.InvoiceDraft`), `amount`;
  - `PaymentOut`: `number`, `account`, `cash_account`, `amount`, `method`,
    `date`, `note`, `cancelled_at`;
  - `ManualEntry`: `account`, `direction`, `amount`, `date`, `reason`,
    `cancelled_at`;
  - `Transfer`: `kind` (`account` or `cash`), `from_account`, `to_account`,
    `from_cash`, `to_cash`, `amount`, `date`, `note`, `cancelled_at`;
  - `OpeningBalance`: `account` or `cash_account`, `direction`, `amount`,
    `date`, `cancelled_at`;
  - `DocumentCounter`: `company`, `prefix`, `last`; unique `(company, prefix)`.
- Produces, in services (every one inside `transaction.atomic`, posting
  through `posting`):
  - `record_collection(*, account, cash_account, amount, method, date, note, applies: list[tuple[InvoiceDraft, Decimal]], by) -> Collection`;
  - `cancel_collection(c, *, by) -> Collection`;
  - `record_payment_out(...) -> PaymentOut` and `cancel_payment_out`;
  - `record_manual_entry(...)`, `record_transfer(...)` and
    `record_opening(...)`, each with its `cancel_*`;
  - `next_number(company, prefix) -> str`, giving `"T-000001"`.
  - `invoice_collected(draft) -> Decimal` and `invoice_remaining(draft) -> Decimal`,
    computed over applications of non-cancelled collections.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_collection_credits_the_account_and_fills_the_till`.
  - `test_numbers_are_sequential_per_firm`: two firms both start at
    `T-000001`.
  - `test_applying_more_than_remaining_is_refused` →
    `OVERPAYMENT_ON_INVOICE`.
  - `test_an_unallocated_collection_is_allowed`.
  - `test_applies_lock_the_invoice`: patch `InvoiceDraft.objects.select_for_update`
    with a spy and assert it was called.
  - `test_payment_out_debits_and_empties_the_till`.
  - `test_cash_transfer_moves_between_tills_only`: no party movement.
  - `test_account_transfer_needs_one_currency`.
  - `test_opening_only_before_other_movements`: an opening after a movement
    is refused (409 `ALREADY_REVERSED` isn't it; use the field error
    `opening_not_first`). Ruling: this is the generic `invalid` code on
    `date`, with the message "Opening balance must come first". Record it in
    the ledger.
  - `test_cancel_posts_reversals_and_only_once`, for each document type.
- [ ] **Steps 2–4** as usual.
- [ ] **Step 5: Commit:** `SL-179-feat: collections, payments out, entries,
  transfers and openings`.

### Task 4: M7 posts to the ledger

**Files:**
- Modify `apps/billing/services.py`:
  - `issue` posts the debit through `ledger.services.post_invoice(draft, by)`;
  - `cancel` of an issued draft posts the reversal, or raises
    `Conflict(INVOICE_HAS_COLLECTIONS)` when collections are applied;
  - `mark_paid(draft, *, paid_on, by, cash_account=None, method="transfer")`
    becomes a full collection of the remaining balance (the default cash
    account is "Merkez kasa").
- The draft's status follows the collections: `paid` when
  `invoice_remaining == 0`, otherwise `issued`. `paid_on` is the covering
  collection's date. Cancelling a collection reopens.
- Modify the M7 read serializer: `collected` and `remaining`.
- Data migration: post the existing issued and paid drafts once. Paid drafts
  get a collection into a "Devir (taşınan ödemeler)" cash account, dated
  `paid_on`. The migration is idempotent.
- Test: `tests/test_ledger.py` class `TestInvoices`, and keep
  `tests/test_billing*.py` green.

**Interfaces:**
- Produces, in `apps.ledger.services`:
  - `post_invoice(draft: InvoiceDraft, *, by: User | None) -> Movement | None`
    (None when already posted);
  - `reverse_invoice(draft, *, by) -> None`;
  - `sync_invoice_status(draft) -> InvoiceDraft`.

- [ ] **Step 1: Write the failing tests:**
  - `test_issuing_posts_one_debit`.
  - `test_reissue_or_retry_posts_nothing_more`: call `post_invoice` twice;
    one movement exists.
  - `test_cancelling_an_issued_invoice_reverses_it`.
  - `test_cancelling_with_collections_is_refused`.
  - `test_partial_collection_leaves_it_issued_with_remaining`.
  - `test_the_covering_collection_marks_it_paid`.
  - `test_cancelling_a_collection_reopens_the_invoice`.
  - `test_mark_paid_is_a_full_collection_into_merkez_kasa`.
  - `test_the_migration_posts_existing_drafts_once`: run it twice.
- [ ] **Steps 2–4,** plus the M7 suites.
- [ ] **Step 5: Commit:** `SL-179-feat: invoices post to the customer's
  account, and payments settle them`.

### Task 5: The API

**Files:** `apps/ledger/api/v1/{serializers,views,urls}.py`, the URL
include, the M7 views (`mark-paid` deprecated in the schema), and
`tests/test_tenant_isolation.py` records. Test class `TestApi`.

**Interfaces:** the endpoints in the spec's API table except the statement
(Task 6). The basenames are `ledger-account`, `ledger-collection`,
`ledger-payment-out`, `ledger-manual-entry`, `ledger-transfer`,
`ledger-opening`, `ledger-cash-account`, `ledger-supplier` and
`ledger-finance` (the summary action).

- [ ] **Step 1: Write the failing tests:**
  - `test_accounts_list_with_balance_and_totals_and_filters` (kind, search,
    has_balance, overdue with `overdue_days=30`).
  - `test_a_retried_collection_posts_once`: the same key twice gives one
    collection and one movement.
  - `test_document_create_and_cancel_round_trip` (parametrised over the
    document endpoints).
  - `test_permissions`:
    - a technician gets 403 everywhere;
    - operations can create a collection, and gets 403 on a manual entry;
    - admin gets 403 on an opening;
    - the accountant can do everything.
  - `test_cash_account_movements_and_balance`.
  - `test_finance_summary` (today and month: receivables, payables, cash
    balances, in and out).
  - `test_a_firm_never_sees_anothers_ledger`, through the isolation suite
    entries.
- [ ] **Steps 2–4,** then **Step 5: Commit:** `SL-179-feat: the ledger over
  HTTP`.

### Task 6: The statement

**Files:** `apps/ledger/statement.py` (rows with a running balance, CSV,
HTML/PDF), `templates/ledger/statement.html`, the
`templates/email/statement/*` mail, and the actions on the account viewset.
Test class `TestStatement`.

**Interfaces:**
- `statement_rows(account, *, start: date | None, end: date | None) -> Statement`,
  where the dataclass holds `opening`, `rows[(date, description, debit, credit, balance, document_type, document_id)]`
  and `closing`.
- `statement_csv(statement) -> str` follows the M7 CSV conventions: BOM, `;`
  separator, decimal comma.
- `render_statement_pdf(statement, *, company) -> bytes`, which reuses the
  billing `PdfRenderingUnavailable`.
- `send_statement(account, *, start, end, by) -> list[str]` sends to the
  customer's contacts with `receives_monthly_report` and a non-blank e-mail.
  With none, it raises `Conflict(REPORT_NO_RECIPIENT)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_running_balance_and_opening_at_from`.
  - `test_csv_matches_json` (same numbers, decimal comma, BOM).
  - `test_html_escapes_and_lists_every_row`.
  - `test_pdf_renders`: skip only when WeasyPrint can't be imported.
  - `test_send_goes_to_report_recipients_with_the_pdf`.
- [ ] **Steps 2–4,** then **Step 5: Commit:** `SL-179-feat: a statement on
  screen, in CSV and PDF, and by mail`.

### Task 7: Demo, contract, gate

- [ ] **Demo:** a step in `core/demo_activity.py` that is idempotent per
  subject. It posts the demo's issued and paid drafts (through the
  migration helper), creates two suppliers with payments out, a bank cash
  account, partial collections on a few customers, and one customer overdue
  by more than 30 days. It adds `ledger +N` to the refresh output. Tests:
  after seeding, every account's balance equals the sum of its movements,
  the finance summary is non-zero, and a second refresh gives `ledger +0`.
- [ ] Run `make spec` and the full gate.
- [ ] **Commit:** `SL-179-feat: the demo keeps its books` (and the contract
  chore if needed). Don't push; the controller opens the PR after the
  branch review.
