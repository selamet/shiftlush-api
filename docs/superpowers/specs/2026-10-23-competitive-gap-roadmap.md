# Competitive gap roadmap: from maintenance records to running the firm

Date: 2026-10-23. A Phase 3 roadmap, written after watching Asansorex's
product video (asansorex.com/tanitim, 7 minutes, 24 features). Every feature
it shows that ShiftLush lacks is planned here as a milestone. Each milestone
gets its own design document before it is built, as Phase 2's did.

## What the video showed, and where ShiftLush stands

Asansorex pitches itself as "one program for the whole firm". Its centre of
gravity is **money in the field**:

- the technician closes a visit or a fault and sees total / collected /
  remaining;
- they take the payment on the spot;
- a pocket thermal printer hands the customer a receipt;
- the remainder lands on the customer's account (cari) by itself.

Around that it has a small accounting suite (accounts, cash and bank,
cheques, e-invoice), stock, configurable checklists, SMS and WhatsApp, live
technician location, an installation-project pipeline, and an AI assistant.

| Area | Asansorex | ShiftLush today |
|---|---|---|
| Field collection and receipts | yes, the headline feature | none; invoice drafts are marked paid by hand (M7) |
| Customer, supplier and staff accounts (cari), statement | yes | none |
| Cash, bank and card accounts, finance screen, day-end | yes | none |
| Cheques and promissory notes (çek/senet) | yes | none |
| e-Fatura / e-Arşiv | yes | none (a Phase 2 non-goal) |
| Stock: parts, movements, critical level | yes | free-text "parts used" on faults |
| Checklist | 53 items, by elevator type | 16 fixed items |
| Signatures | technician and customer, rating, comment | customer signature (just shipped) |
| Thermal printer: visit and fault slips, payment receipt, QR label | yes | QR labels as PDF only |
| SMS, WhatsApp, bulk SMS | yes | e-mail only |
| Per-visit A4 PDF and signed-document link | yes | none (the monthly report is in progress) |
| Live technician location, location check-in | yes (KVKK consent) | route order only |
| Installation projects and new-lift orders | yes | none |
| AI assistant over the firm's data | yes | none |
| Excel import, bulk lift add | yes | none |

ShiftLush has things the video doesn't show:
- periodic inspection with legal correction deadlines;
- the customer portal;
- the contract lifecycle (renewal, expiry, auto-renewal);
- the offline visit outbox;
- role-based permissions;
- the calendar;
- the monthly customer report.

These are kept, and the new milestones build on them rather than around
them.

## Principles carried over

- Every milestone has its own design document, an API PR and a web PR.
- Money is exact: `Decimal`, two places, one currency per account. A posted
  movement is never edited, only reversed by a counter-movement.
- Nothing customer-facing goes out until the firm switches it on (the
  monthly-report rule): SMS, WhatsApp and e-invoice.
- Field features work offline through the existing outbox.
- Turkish only in templates and data, as before.

## The milestones

The order follows dependencies. Sizes are S (a few days), M (one to two
weeks) and L (two to four weeks) of the pace so far.

### F0 — The ledger: accounts, cash and bank (L), the foundation

**Why:** Collection, receipts, statements, finance reports, cheques, stock
purchases and e-invoice all need one place where money is recorded. Without
it each feature would invent its own balance and none would agree.

**Scope:**
- **`Account` (cari):** one per party.
  - kind `customer` (created automatically with every customer), `supplier`
    or `staff`;
  - opening balance and currency;
  - balance is computed, never stored as truth (a cached column is allowed,
    if it is rebuilt from movements and checked in tests).
- **`CashAccount` (kasa):** kind `cash`, `bank` or `card`, with a name and
  currency. A firm may hold several, for example "Merkez kasa", "Ziraat
  IBAN" and one per technician's pocket (see F1).
- **`Movement`:** an immutable ledger row: date, account, debit or credit,
  amount, source (invoice, collection, payment out, manual, transfer,
  opening), a link to the source document, and the user.
  - Reversal is a new row pointing at the one it reverses.
  - Cash moves are also posted to a `CashAccount`.
- **What posts automatically:**
  - an invoice draft marked **issued** (M7) posts a debit on the customer's
    account;
  - **cancelling** an issued draft posts the reversal;
  - "mark paid" in M7 becomes a collection (F1) instead of a date field.
    Existing `paid_on` values migrate as collections into a "migration" cash
    account.
- **Manual entries:** debit or credit with a reason (opening, correction, or
  "manuel borç/alacak" in the video), and a transfer between two accounts
  (virman). Manual entries are for the owner and accountant only.
- **Statement (ekstre):** dated movements with a running balance, filtered
  by date, as PDF or CSV, and e-mailed to the account's contacts.
- **Finance screen:** receivables, payables, each cash account's balance,
  and today / yesterday / this week / this month.

**Out of scope:** a full chart of accounts or double-entry bookkeeping for
the firm's own books. ShiftLush is a pre-accounting tool (ön muhasebe). The
firm's accountant still keeps the legal books.

**Decisions for the user:**
- Does an issued invoice post the debit, or does each completed visit, as in
  Asansorex? Recommended: the invoice, because the contract is the unit of
  billing in ShiftLush already.
- Multi-currency: allow it per account, or stay TRY-only for now?

**Acceptance:**
- A customer's balance always equals the sum of their movements.
- Issuing, cancelling and collecting move it as expected.
- The statement PDF matches the screen.

### F1 — Collecting in the field (M), needs F0

**Why:** It is the video's centre, and the daily reality of small lift firms:
the technician leaves with cash.

**Scope:**
- After a visit or a fault is recorded, a **"Tahsilat al"** step shows the
  account's balance and what this job adds. It takes an amount, a method
  (nakit, havale/EFT, kredi kartı, çek in F6), a target cash account and a
  note.
- **Technician custody:** cash the technician takes goes to their own pocket
  cash account. The office records a **handover** (teslim) that moves it to
  the main cash account. "Who holds how much" is always on screen.
- **Collection receipt (makbuz):** numbered per firm, shown as a PDF, and
  printed in F2. Its link opens the signed document, see F3.
- **Offline:** a collection is queued in the outbox with the visit, under
  one idempotency key per collection.
- **Fault pricing:** the fault's labour and parts (F4) produce a priced
  service line that posts to the account when the fault closes as billable.
  It replaces "needs price" in M7 for faults priced in the field.
- **Permissions:** technicians may collect, but not edit or delete a
  collection. Correcting one is an office reversal.

**Decisions for the user:**
- May technicians collect at all, and with an upper limit per collection?
- Should card payments go through a POS integration (iyzico or PayTR link),
  or only be recorded as "paid by card"? Recommended: record only at first.

**Acceptance:**
- A collection taken offline arrives once.
- The customer's balance and the technician's pocket move together.
- A handover empties the pocket into the main cash account.

### F2 — Thermal printer (M), needs F1 for receipts

**Why:** The video's most visible moment. A paper slip in the customer's
hand at the lift replaces the paper form, the receipt book and the label
printer.

**The hard constraint:** ShiftLush is a PWA.
- **Android Chrome** can talk to a Bluetooth ESC/POS printer directly
  (Web Bluetooth).
- **iPhone Safari cannot.** Apple doesn't support Web Bluetooth in any iOS
  browser.

**Options:**
1. **Android-only direct printing** through Web Bluetooth and ESC/POS. iPhone
   users print through the printer vendor's app by sharing a PDF sized to
   58 or 80 mm. Cheapest; uneven on iPhone.
2. **A thin native shell (Capacitor) around the PWA** for iOS and Android,
   with a Bluetooth plugin. Equal on both; app-store accounts, review and
   release overhead.
3. **Print through a cloud print service.** Rare for pocket printers; not
   recommended.

**Recommended:** option 1 now, option 2 if iPhone technicians are common
among customers.

**Scope (any option):**
- An ESC/POS document builder for 58 and 80 mm: Turkish characters (code
  page 857 or raster), bold, sizes, and QR codes.
- The documents:
  - **bakım fişi:** checklist summary, findings, parts, the money
    (total / collected / remaining) and the signature state;
  - **arıza servis fişi:** reported, assigned and resolved times, work done,
    labour and parts;
  - **tahsilat makbuzu**;
  - **QR etiket:** firm name, lift name, and the QR to the public fault page.
- Each slip carries a short link to the signed document online (F3).
- A per-device printer setting: pair the printer, choose the paper width,
  and print a test page.

**Decision for the user:** option 1 or option 2, and which printer models to
support first? The video shows a "MileStone" pocket printer; any ESC/POS
58/80 mm model should work.

### F3 — Per-visit documents and a public signed link (S–M), needs the signature (done)

**Scope:**
- **An A4 PDF "Asansör Bakım Formu" for every visit.** It has the checklist
  with results, the findings, the parts, the technician, and the
  signature(s).
- **An A4 fault service report** with the same parts.
- **A public, unguessable, revocable link** (`/d/<token>`) that shows the
  signed document read-only, with no login. It is what the thermal slip and
  SMS point to, and it expires or can be revoked by the office.
- **A technician signature** next to the customer's. The visit signature
  milestone already holds the customer's.
- **Customer rating (1–5) and comment,** optional, captured with the
  signature and shown in the monthly report and the office.

**Acceptance:**
- The link works without a login and never reveals another record.
- Revoking it returns 404.

### F4 — Stock and parts (M–L), needs F0 for purchases

**Scope:**
- **`Part` catalogue:** code, name, unit, sale price, cost, VAT and a
  critical level.
- **Stores:**
  - "Depo" (the main store);
  - one per technician's van (araç stoğu);
  - movements for purchases (from a supplier account, posting to F0),
    transfers between stores, use on a fault or visit, adjustments and
    counts.
- **Using parts in the field:**
  - Faults and visits use parts from the technician's van with a quantity.
  - The price flows into the fault's billable line (F1) and the invoice
    draft.
  - Stock drops at once.
  - Offline use is queued.
- **Warnings:** a critical-level notification, and a stock report with
  value at cost.
- **Migration:** the old free-text `parts_used` is kept on old faults and
  shown as text.

**Decision for the user:** do technicians have van stock, or does
everything come from one store?

### F5 — Configurable checklists (M)

**Why:** 16 fixed items are a starting point. Firms and lift types differ:
electric traction, hydraulic, MRL (machine-room-less), goods lifts and
escalators.

**Scope:**
- **`ChecklistTemplate`:** a lift type, categories, and items with an
  order, a "requires note on failure" flag, and an optional photo
  requirement.
- **A ShiftLush default set of about 50 items,** grouped by category (the
  video groups by area: machine room, car, shaft, pit, doors, safety).
  Written against TS EN 13015 and the lift maintenance regulation, and
  reviewed by a person in the trade before it ships.
- **Firms can copy and edit templates.** Each lift gets a type, and the type
  picks its template.
- **Versioning:** a visit stores the template version it used, so editing a
  template never rewrites past visits. The 16 existing items map onto the
  default set, so old visits read correctly.
- **"Mark section all OK",** as in the video, while still requiring an
  answer per item for completed visits (M1's rule stays).

**Decision for the user:** who writes and approves the default list: you,
or a maintenance engineer you know?

### F6 — Cheques and promissory notes (S–M), needs F0

**Scope:**
- **Notes received from customers or given to suppliers:** number, bank,
  due date, amount, drawer and status. Statuses: portföyde (in hand),
  tahsile verildi (sent for collection), ciro edildi (endorsed), tahsil
  edildi (collected), karşılıksız (bounced), iade (returned).
- **Postings:** each status change posts to F0. Receiving a note is a
  collection by cheque; collecting it moves money into a bank account.
- **A due-date calendar** and a notification before the due date.

### F7 — Messages: SMS, WhatsApp, bulk (M)

**Scope:**
- **An SMS provider integration** (Netgsm, İleti Merkezi or Mutlucell) with
  the firm's sender name (başlık).
  - It is used for: the visit-done SMS with the document link, the fault
    received/assigned/resolved SMS to the reporter, payment reminders, and
    renewal notices.
  - There's a balance and credit display.
- **WhatsApp:**
  - **Phase 1:** a "send via WhatsApp" button that opens WhatsApp with a
    prepared message and link (click-to-chat). It needs no approval and
    costs nothing.
  - **Phase 2 (optional):** the WhatsApp Business API, for automatic
    messages. Meta approval and cost.
- **Bulk SMS** to a filtered customer list, with the İYS (Ileti Yönetim
  Sistemi) rules for commercial messages. Service messages are exempt;
  marketing needs consent recorded in İYS.
- **Firm controls:** every event has its own switch, off by default, and
  templates in Turkish.

**Decisions for the user:** which SMS provider (who pays: the firm with its
own account, or ShiftLush resells credit)? And is click-to-chat WhatsApp
enough at first?

### F8 — Finance reports (S–M), needs F0, F1

**Scope:**
- **Gün sonu (day-end):** money in and out by cash account and by user.
- **Borç yaşlandırma (aging):** receivables bucketed by 0–30, 31–60, 61–90
  and 90+ days, per customer.
- **Personel tahsilatı:** what each technician collected and handed over.
- **Bakım matrisi:** lifts × months, done or missed. The plan and visits
  already hold this.
- **Bakım karlılığı:** contract income against labour and parts per
  customer. This comes later and needs F4.
- **Export:** every report as PDF and CSV (the M7 conventions: BOM, `;` and
  decimal comma).

### F9 — e-Fatura / e-Arşiv (L), needs F0, an integrator decision

**Scope:**
- **An integration with one GİB özel entegratör** (for example Sovos/Foriba,
  Uyumsoft, Logo, Mysoft or Paraşüt's API). It covers:
  - sending e-Fatura to e-invoice taxpayers and e-Arşiv to everyone else;
  - checking the recipient's status;
  - PDF/XML retrieval;
  - status tracking (sent, accepted, rejected);
  - cancellation within the legal window.
- **Issuing:** an issued M7 draft becomes the source of the e-invoice. The
  invoice number comes from the integrator, replacing the hand-typed one.
- **Per-firm credentials** to the integrator, stored encrypted.
- **Portal:** customers see their e-invoices in the portal (this closes M6's
  "invoices in the portal" non-goal).

**Decision for the user:** which integrator, and does ShiftLush hold the
contract (reselling) or does each firm bring its own? This decision gates
the whole milestone.

### F10 — The field team: live location and check-in (M)

**Scope:**
- **Location check-in.** Starting a visit or arriving at a fault records
  the phone's position. It is compared with the building's (the B part of
  the calendar milestone gave buildings positions), and the visit is marked
  "on site, verified" or "away from building". The office sees mismatches
  in a "Konum onayları" list.
- **Live location, optional, per firm and per technician:**
  - shared only during working hours and only after **explicit consent**
    (açık rıza);
  - an aydınlatma metni (privacy notice) shown on the phone;
  - retention of 30 days, then deleted;
  - a map of who is where for the office.
  - A PWA can only report while it is open, so true background tracking
    needs the F2 native shell.
- **Routes:** today's route (already built) shown on the same map.

**Decision for the user:** is check-in enough, or is live location wanted
despite the KVKK and battery cost?

### F11 — Installation projects and new-lift orders (L)

**Why:** Many maintenance firms also install lifts. Today that work lives in
spreadsheets.

**Scope:**
- **`Project` for a new installation or a modernisation.** Stages: teklif
  (quote), sözleşme (contract), proje/onay (design and permit), üretim/sipariş
  (manufacture and order), montaj (installation), test/CE, teslim (handover).
  Each stage has dates, responsible people, documents, and a checklist.
- **A payment schedule:** advance and instalments, posting to F0 as they
  fall due.
- **Material orders** from suppliers, through F4 purchases.
- **Handover:**
  - the delivered lift becomes an `Elevator` with its CE documents;
  - a maintenance contract is offered;
  - the first periodic inspection due date is set.

### F12 — The assistant (M)

**Scope:**
- **An in-app chat that answers questions about the firm's data.** For
  example "bugünün özeti", "açık arızalar", "Murat Demir carisinin bakiyesi"
  or "bu hafta bakımlar kimde".
- **Read-only** in the first version.
- **Built on Claude with tool calls** to existing services, scoped to the
  user's role and firm.
- **Every answer cites the records it used** and links to them. Nothing is
  answered from outside the firm's data.
- **Usage limits per firm.** The data processing is covered in the KVKK
  documentation.

**Decision for the user:** is it worth it now? It demos well, but adds a
running cost per question.

### F13 — Onboarding: Excel import and bulk add (S–M)

**Scope:**
- **An import wizard** for customers, buildings, lifts, contracts, contacts
  and opening balances (F0). It works from a downloadable Excel template,
  with a preview, row-level errors, and a dry run before commit.
- **"Toplu asansör ekle" on a building:** N lifts with a name pattern and
  shared fields.
- **A per-firm support channel link** (the video's WhatsApp group) in
  settings. This is a process, not a feature.

## Order and dependencies

```
F13 (onboarding) ─────────────────────────────── any time, best early
F5 (checklists) ──────────────────────────────── independent
F3 (documents, link, rating) ─┐
F0 (ledger) ──► F1 (collect) ─┼──► F2 (thermal printer)
          ├──► F6 (cheques)   │
          ├──► F4 (stock) ────┘
          ├──► F8 (reports)
          └──► F9 (e-invoice, after the integrator decision)
F7 (SMS/WhatsApp) ── after F3 (it sends the document link)
F10 (location) ── after F2 if live tracking needs the native shell
F11 (projects) ── after F0 and F4
F12 (assistant) ── last; it reads everything above
```

**Suggested first wave**, about 6–8 weeks at the pace so far: **F0 → F1 → F3
→ F2**. This closes the video's headline loop. The visit is signed, money is
collected, the slip is printed, and the customer gets a link. That's where a
firm comparing the two products would decide.

**Second wave:** F5, F4, F8 and F13. **Third:** F7, F6, F9 and F10.
**Fourth:** F11 and F12.

The monthly customer report (in progress) and offline reads (planned) stay
in the queue and are finished first.

## Decisions needed before each wave

| Before | Decision |
|---|---|
| F0 | Does an issued invoice post the debit (recommended), or each completed visit? TRY only or multi-currency? |
| F1 | Can technicians collect, and with what limit? Card: record only, or a POS link? |
| F2 | Android-only Web Bluetooth with a PDF fallback on iPhone, or a native shell? Which printers? |
| F4 | Van stock per technician, or one store? |
| F5 | Who writes and approves the default 50-item list? |
| F7 | Which SMS provider, and who pays? Is click-to-chat WhatsApp enough at first? |
| F9 | Which e-invoice integrator, and who holds the contract? |
| F10 | Check-in only, or live location too? |
| F12 | Build the assistant now, or later? |

## Non-goals

- Becoming a general accounting package. The legal books stay with the
  firm's accountant, and ShiftLush exports what they need.
- Payroll and HR.
- A marketplace or a residents' app.
