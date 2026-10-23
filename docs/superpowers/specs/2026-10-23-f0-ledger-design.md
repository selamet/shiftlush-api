# F0 — The ledger: accounts, cash and bank

Date: 2026-10-23. Roadmap: F0 in `2026-10-23-competitive-gap-roadmap.md`
(issue #140). It builds on M7 (invoice drafts) and is the foundation for F1
(collecting in the field), F2 (receipts), F4 (stock purchases), F6
(cheques), F8 (finance reports), F9 (e-invoice) and F11 (project payments).

It was written with the defaults the roadmap recommended, at the user's
request ("open the issues and start in order"). The user reviews it before it
is built. Every decision is a default unless it says otherwise.

## Why

Today ShiftLush knows what a customer **should** pay: M7 drafts an invoice
per contract period, and the accountant issues it. It doesn't know what the
customer **has** paid, beyond a single "paid on" date per draft. Nothing else
is recorded:

- a customer who pays half;
- a customer who pays three invoices at once;
- cash a technician took at the door;
- what the firm owes its parts supplier;
- how much is in the till.

A lift firm's day turns on exactly these questions, and today it answers them
in a separate pre-accounting program or a notebook.

F0 gives every party an **account** (cari) and every money event a
**movement**. A balance is never typed in. It is always the sum of what
happened.

## Goals

- Every customer, supplier and staff member has an account, and its balance
  is always the sum of its movements.
- Issuing, cancelling and collecting an invoice move the customer's balance
  without anybody typing a number twice.
- Money held is visible per cash account (cash, bank, card), with what came
  in and went out today.
- A statement (ekstre) per account, on screen, as PDF and CSV, and e-mailed to
  the customer's contacts.
- Mistakes are corrected by reversal, never by editing, so the history is the
  audit trail.

## Non-goals

- The firm's legal books: no chart of accounts, VAT returns or general
  ledger. ShiftLush is a pre-accounting tool (ön muhasebe), and the
  accountant keeps the books. A CSV export is what they get.
- Collecting in the field from the technician's phone. That is F1, which
  builds on these documents.
- Cheques and notes (F6), e-invoice (F9) and stock purchases (F4).
- Currency conversion. Each account and each cash account has one currency.
  A movement must match both, and there are no exchange rates.
- Bank-statement import or reconciliation.

## Anti-goals (it fails if…)

- …a balance can disagree with its movements, or be typed in.
- …a posted movement can be edited or deleted.
- …the same invoice posts twice (a retried issue, a re-run migration).
- …one firm's accounts, movements or balances are visible to another.
- …a technician or operations user can make a manual entry.

## The model (a new app, `apps.ledger`)

### `Account` (cari hesap)

| Field | Meaning |
|---|---|
| `company` | Tenant. |
| `kind` | `customer`, `supplier` or `staff`. |
| `customer` | FK, set for `customer`. Unique: one account per customer. |
| `supplier` | FK to a new `Supplier`, set for `supplier`. Unique. |
| `user` | FK, set for `staff`. Unique. |
| `name` | Copied from the party at creation, and kept in step when it is renamed. |
| `currency` | ISO code, default `TRY`. Fixed once the account has a movement. |
| `is_active` | Inactive accounts are hidden from pickers, never deleted. |

- A check constraint ensures exactly one party field is set, matching
  `kind`.
- **A customer's account is created with the customer** (service plus data
  migration for existing customers). Supplier and staff accounts are created
  when the supplier is created or a staff member is first given one.
- The **balance** is computed: the sum of debits minus the sum of credits. A
  positive balance means they owe the firm. The list reads it through one
  annotated query, and there is no stored balance column in F0.

### `Supplier`

Name, tax office and number (with the existing validators), phone, e-mail,
address note and notes. It is the minimum F4 needs to buy parts and F0 needs
to record what is owed. It is tenant-scoped, with soft delete.

### `CashAccount` (kasa / banka / kart)

| Field | Meaning |
|---|---|
| `kind` | `cash`, `bank` or `card`. |
| `name` | "Merkez kasa", "Ziraat — TR12…", "Kredi kartı POS". |
| `currency` | Fixed once used. |
| `iban` | Optional, for `bank`. |
| `is_active` | |

Every firm gets one `cash` account named "Merkez kasa" (data migration and
company registration). The cash account's balance is computed from its
movements, the same way.

### `Movement` (hareket) — the only place money is recorded

| Field | Meaning |
|---|---|
| `account` | The party side. Nullable only for pure cash transfers. |
| `cash_account` | Set when real money moved in or out of the firm. |
| `date` | The business date (Istanbul), editable at creation, never after. |
| `direction` | `debit` (the party owes more) or `credit` (the party owes less). |
| `amount` | `Decimal(14,2)`, > 0. |
| `currency` | Must equal the account's, and the cash account's. |
| `cash_direction` | `in` or `out` for the cash account, when set. |
| `source` | `invoice`, `invoice_cancel`, `collection`, `payment_out`, `manual`, `transfer`, `opening` or `reversal`. |
| `document_type` / `document_id` | The source document (invoice draft, collection, …). |
| `reverses` | FK to the movement this one cancels. Unique, so a movement can only be reversed once. |
| `description` | Shown on the statement. |
| `created_by` | |

- **Immutable:** there is no update, and no delete through any API. The admin
  site is read-only for it.
- **One invoice posts once:** a unique constraint on
  `(source, document_type, document_id)` for the sources `invoice` and
  `invoice_cancel`.

### Documents that post movements

- **`Collection` (tahsilat).** Fields: number (per firm, sequential, prefix
  `T-`), account, cash account, amount, method (`cash`, `transfer`, `card`),
  date, note, the invoice drafts it pays (optional, many-to-many with the
  amount applied), and `created_by`.
  - It posts one credit on the account, with cash in.
  - Cancelling it posts the reversal. A collection is never edited.
- **`PaymentOut` (tediye).** The mirror image: money out to a supplier or a
  staff member. Debit on the account, cash out. Number prefix `O-`.
- **`ManualEntry` (manuel borç / alacak).** Account, direction, amount, date
  and a required reason. No cash. Owner and accountant only.
- **`Transfer` (virman).**
  - **Between two accounts** of the same currency: a credit on one and a
    debit on the other, for example moving a balance to a related company's
    account.
  - **Between two cash accounts:** cash out of one and in to the other, for
    example depositing the till into the bank.
- **`OpeningBalance` (devir).** One per account or cash account, at go-live.
  Owner and accountant only. It can be reversed and entered again before the
  first other movement.

## How M7 posts

| M7 event | Movement |
|---|---|
| A draft is **issued** | A debit on the customer's account for `total`, source `invoice`, dated the invoice date, with the description "Fatura {no} — {dönem}" ("Invoice … — period"). |
| An issued draft is **cancelled** | A credit reversing that debit, source `invoice_cancel`. Allowed only while no collection is applied to it; otherwise reverse the collection first (`409 INVOICE_HAS_COLLECTIONS`). |
| **Mark paid** | Becomes "record a collection". The accountant picks the cash account and method, and can change the amount and date, with the draft's remaining balance as the default. The draft reads `paid` when collections cover its total, and `issued` with a remaining amount otherwise. `paid_on` is the date of the collection that covered it. |

The decision for the user said "invoice posts the debit (recommended) or
each visit". This document takes the invoice, which matches the contract as
the billing unit.

**Migration of existing data, idempotent:**

- every issued or paid draft posts its debit;
- every paid draft gets a collection of its total, dated `paid_on`, into a
  cash account named "Devir (taşınan ödemeler)" (carried-over payments),
  method `transfer`, note "M7'den taşındı" ("carried over from M7");
- cancelled drafts post nothing.

The accountant can move those collections to the right cash account with a
transfer.

## Rules

- **Amounts:** greater than zero, two places, one currency end to end.
  Mismatches are `400 CURRENCY_MISMATCH`.
- **A movement's date** can't be after today. A date before the firm's
  "closed through" date can't be used either: an owner setting, empty by
  default, that freezes past months (`400 PERIOD_CLOSED`).
- **Reversal:** any document can be cancelled once, which posts the reversal
  of each of its movements. A second cancel is `409 ALREADY_REVERSED`.
  Reversing a reversal isn't offered; the document is entered again instead.
- **Collections applied to invoices** can't exceed an invoice's remaining
  balance (`400 OVERPAYMENT_ON_INVOICE`). A collection with no invoice
  applied is allowed: it is an advance or payment on account and shows as
  "mahsup edilmemiş" (unallocated).
- **Permissions** (new matrix resources):

  | | read | collections / payments out | manual, transfer, opening, closed-through |
  |---|---|---|---|
  | owner | ✓ | ✓ | ✓ |
  | admin | ✓ | ✓ | — |
  | accountant | ✓ | ✓ | ✓ |
  | operations | balances only (the customer page) | ✓ | — |
  | technician | — (F1 adds field collections) | — | — |

## API (`/api/v1/ledger/…`)

| | |
|---|---|
| `GET /accounts/` | Filters: `kind`, `search`, `has_balance` and `overdue` (owes and has an issued invoice older than N days). Each row has its balance, and the list carries totals. |
| `GET /accounts/{id}/` | The party, the balance, and the last movement. |
| `GET /accounts/{id}/statement/?start=&end=` | Dated movements with a running balance and the opening balance at `start` (`end` inclusive). The same data is available as `.pdf` and `.csv`, and through `POST …/statement/send/`, which e-mails the PDF to the customer's contacts who receive reports. |
| `GET/POST /collections/`, `POST /collections/{id}/cancel/` | Create takes an `Idempotency-Key`. Its body is account, cash account, amount, method, date, note, and `applies: [{invoice, amount}]`. |
| `GET/POST /payments-out/`, `POST /payments-out/{id}/cancel/` | |
| `POST /manual-entries/`, `POST /transfers/`, `POST /opening-balances/` (and cancel on each) | |
| `GET/POST/PATCH /cash-accounts/`, `GET /cash-accounts/{id}/movements/` | |
| `GET/POST/PATCH /suppliers/` | |
| `GET /finance/summary/?period=today\|yesterday\|week\|month` | Receivables, payables, each cash account's balance, and money in and out in the period. |
| M7 | `POST /invoice-drafts/{id}/mark-paid/` is replaced by `POST /collections/` with `applies`. The old endpoint is kept, deprecated, and creates a full collection into a cash account given in the body, so the web keeps working until it moves. Drafts gain `collected` and `remaining`. |

New error codes: `CURRENCY_MISMATCH`, `PERIOD_CLOSED`, `ALREADY_REVERSED`,
`INVOICE_HAS_COLLECTIONS` and `OVERPAYMENT_ON_INVOICE`.

## Web

- **Nav, under "Finans":** Cariler, Kasa & Banka, Tedarikçiler.
- **Cariler:** a list with balance, kind and an "overdue" filter, and totals
  at the top.
- **Cari detail:**
  - the balance large;
  - buttons for Tahsilat al, Tediye, Manuel borç/alacak and Virman;
  - the statement with a date range, PDF, CSV and "E-posta ile gönder";
  - each row links to its source document, which can be cancelled with a
    confirmation dialog that shows what the reversal posts.
- **Customer page:** the balance and "Ekstre" (statement) link in the header,
  for the roles that may read it.
- **Kasa & Banka:** each account's balance, its movements, and "Kasalar arası
  virman" (transfer between cash accounts).
- **Finans** (home of the section): receivables, payables and cash balances,
  with today, yesterday, this week and this month.
- **Fatura taslakları (M7):** "Ödendi işaretle" becomes "Tahsilat kaydet"
  (record a collection) with the remaining balance prefilled. Each draft
  shows collected and remaining.
- **Settings:** "Kapanmış dönem" (closed-through date, owner only), and the
  opening balances at go-live through a guided screen.

## Demo

The demo firm gets:
- its issued and paid drafts posted;
- two suppliers with a few payments out;
- a bank account and the Merkez kasa with plausible balances;
- some customers with partial payments and one overdue customer, so the
  aging and finance screens have something to show.

## Delivery

1. **API, part 1:** the app, the models, posting, M7 integration and
   migration, collections, payments out, manual entries, transfers, opening
   balances, cash accounts, suppliers, the statement (JSON, CSV, PDF and
   send), the finance summary, permissions, error codes, the demo and
   OpenAPI.
2. **Web:** the screens above, and moving M7's mark-paid to a collection.

## Acceptance

- [ ] **Balances:** for every account and cash account, the balance equals
      the sum of its movements. A test re-derives it after a random sequence
      of documents and cancellations.
- [ ] **One posting per invoice:** issuing a draft posts one debit, a
      retried issue posts nothing, cancelling posts the reversal, and
      cancelling with a collection applied is refused.
- [ ] **Partial collections:** a partial collection leaves the draft
      `issued` with the right `remaining`. The collection that covers it
      marks it `paid`.
- [ ] **Migration:** it posts every existing issued and paid draft exactly
      once, and a second run posts nothing.
- [ ] **Immutability:** no API path edits or deletes a movement, and a
      cancelled document can't be cancelled again.
- [ ] **Permissions and closed periods:** a technician or operations user
      can't make a manual entry, and a date before the closed-through date
      is refused.
- [ ] **Isolation:** one firm never sees another's accounts, movements or
      totals, enforced by the tenant-isolation suite.
- [ ] **Statement:** the PDF and CSV match the JSON statement for the same
      range.
- [ ] **Checks:** backend `make lint`, `make mypy`, `make check` and
      `make test` pass. Web `npm run verify` passes.

## Calls made without the user

- Issued invoices post the debit, not each visit.
- One currency per account and cash account, with no conversion; TRY by
  default.
- No stored balance. It is computed, with an index on
  `(account, date)`.
- "Mark paid" becomes a collection, and the old endpoint is kept
  deprecated for one release.
- Old paid drafts are carried into a "Devir" cash account rather than
  guessing which till they went to.
- Operations can record collections and payments out, but not manual
  entries, transfers or openings.
- A closed-through date, owner-set and empty by default, freezes past
  months.

## Left to the builder

- Number sequences per firm: a counter row with `select_for_update`, or a
  Postgres sequence per firm.
- Whether documents and movements share one table per document type or a
  base table.
- The statement PDF's layout, following the M7 and monthly-report
  templates.
