# M7 — Billing drafts

Date: 2026-10-21. Roadmap: M7 (`2026-10-20-phase-2-roadmap.md`, #92).
Reads contracts (Phase 1, M2) and closed faults (M4).

## Why

The firm invoices every contract each billing period, plus the repairs a
contract does not cover. Today the accountant rebuilds that by hand every month
from the contract list and memory. The product already knows:
- the fee
- which lifts each contract covered on which days
- the VAT rate
- which faults were closed and billable

M7 turns that into **drafts** the accountant checks, completes and marks as
issued.

**It does not issue invoices.** A Turkish invoice is legally issued through
e-Fatura or e-Arşiv, via the firm's own integrator. That stays out of scope
(roadmap non-goal). The accountant copies the draft into that system and
records the invoice number and date back here.

## The model — `apps.billing`

### `InvoiceDraft`

| Field | Meaning |
|---|---|
| `company`, `contract`, `customer` | Tenant-scoped. The customer is copied from the contract. |
| `period_start`, `period_end` | The billing period, inclusive. |
| `status` | `draft` → `issued` → `paid`, or `cancelled` from `draft` or `issued`. |
| `currency` | From the contract. |
| `vat_rate` | Copied when the draft is generated. |
| `subtotal`, `vat_amount`, `total` | Derived from the lines and stored. They are recomputed on every line change while in `draft`, and frozen at `issued`. |
| `invoice_number`, `invoice_date` | Required to issue. Entered by the accountant from the e-invoicing system. |
| `paid_on` | |
| `notes` | |

Unique (`contract`, `period_start`) with `is_deleted = false`: one draft per
contract per period.

### `InvoiceLine`

| Field | Meaning |
|---|---|
| `draft` | |
| `kind` | `contract_fee`, `elevator_fee`, `fault` or `manual`. |
| `description` | Generated, and editable while in `draft`. |
| `elevator` | Nullable. |
| `fault` | Nullable. |
| `quantity` | Decimal: days or months. |
| `unit_price` | |
| `amount` | Quantity × unit price, rounded half-up to 2 places. |
| `needs_price` | `true` for a fault line until the accountant enters a price. |

## Generation — a daily job, `draft_invoices` (05:00 Istanbul)

For each `ACTIVE` or recently `EXPIRED`/`RENEWED` contract, the job drafts every
billing period that **ended yesterday or earlier** and has no draft yet. The
unique constraint makes it idempotent.

- **Periods.** They are aligned to the contract's `start_date` and stepped by
  `billing_period`:
  - monthly is 1 month, quarterly 3, semiannual 6, annual 12;
  - the month arithmetic is the same as `next_term()`;
  - the last period is cut at `end_date`.
  - Example: a contract starting 15 March is billed 15 March–14 April,
    15 April–14 May, and so on.
- **The fee.** Monthly fees are **prorated by covered days**:
  - `flat`: one `contract_fee` line, `monthly_fee × months in the period`. A
    period cut by `start_date` or `end_date` is prorated by days covered out of
    days in the period.
  - `per_elevator`: one `elevator_fee` line per contract line that was open at
    any point in the period, `unit_price × months × (covered days / period
    days)`. A lift added or removed mid-period pays for the days it was
    covered.
  - A line with no `unit_price` gets `needs_price=true` rather than an invented
    price.
- **Billable faults.** One `fault` line per fault that was **closed within the
  period**, is `billable`, belongs to a lift this contract covered on the day
  it was reported, and has not been drafted before (a fault appears on one
  draft only). Faults carry no price, so the line has `needs_price=true` and
  its description is built from the fault: reference, lift, parts used.
- **VAT.** A contract whose `vat_rate` is unset **gets no draft**. It raises a
  `billing_vat_missing` notification instead, once per (contract, period).
  This is the same refusal SL-85 made for contract totals and M2 for
  auto-renewal.
- **Notifications.** `invoice_drafts_ready` goes to owner, admin and
  accountant, once per run that created drafts: "Ekim 2026 için 12 fatura
  taslağı hazır, 3 tanesinde fiyat eksik".

## Lifecycle

| Action | Who | Rule |
|---|---|---|
| Edit lines, add `manual` lines, edit notes | owner, admin, accountant | `draft` only. The totals recompute. |
| `POST /invoice-drafts/{id}/issue/` | the same | Needs `invoice_number` and `invoice_date`, and **no line with `needs_price`**. Freezes the draft. `invoice_number` is unique per firm among issued drafts. |
| `POST /invoice-drafts/{id}/mark-paid/` | the same | Needs `paid_on`. From `issued` only. |
| `POST /invoice-drafts/{id}/cancel/` | the same | Needs a reason. From `draft` or `issued`. The draft's faults are released, so a regenerated draft can pick them up. |

A step from the wrong state is `409 INVOICE_TRANSITION_NOT_ALLOWED`. A missing
price is `422 INVOICE_LINES_NEED_PRICE`.

## API and exports

| | |
|---|---|
| `GET /invoice-drafts/` | Filters: `status`, `customer`, `contract`, `period` (YYYY-MM overlaps), `needs_price`. |
| `GET` / `PATCH /invoice-drafts/{id}/`, line endpoints | |
| `GET /invoice-drafts/export.csv?…` | The filtered list, one row per draft: customer, contract, period, subtotal, VAT, total, status, invoice number. Semicolon-separated, UTF-8 with a BOM, so Turkish Excel opens it correctly. |
| `GET /invoice-drafts/{id}/pdf/` | A one-page PDF of the draft, rendered with WeasyPrint (already a dependency, used for QR labels). Marked **"TASLAK — fatura değildir"** until issued. |

**Permissions:** a new `invoice` resource. Read and write: owner, admin,
accountant. Operations and technicians: none. This is the first module where
the accountant is the main user.

## Web (separate PR)

- **`/billing`** (accountant, owner, admin): drafts by period, with filters.
  Drafts with a missing price come first. Totals per status.
- **Draft detail:** lines, with inline price entry for `needs_price` lines,
  manual lines, notes, issue (number and date), mark paid, cancel, and the PDF.
- **CSV export** of the current filter.
- The contract detail lists the contract's drafts.
- The accountant's landing page becomes `/billing`.

## Non-goals

- e-Fatura / e-Arşiv (GİB) integration, and invoice numbering.
- Payments, collections and reminders. `paid_on` is recorded by hand.
- Credit notes. A cancelled issued draft is the record; the firm's e-invoicing
  system handles the credit note.
- Multiple currencies on one draft.

## Decisions made without the user

- Periods are aligned to the contract start date.
- Fees are prorated by covered days.
- Fault lines need a price entered by the accountant.
- No draft, and a notification instead, when VAT is unset.
- The CSV is semicolon-separated UTF-8 with a BOM, for Turkish Excel.
- The PDF is marked as a draft until issued.
- The generator runs at 05:00 Istanbul.

## Slices

1. **Backend:** the models, generation, lifecycle, exports, notifications and
   permissions.
2. **Web:** `/billing`, the draft detail, the CSV export, and the drafts on the
   contract page.
