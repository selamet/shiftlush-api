# The monthly customer report

Date: 2026-10-22. Phase 3. Builds on M1 (visits), M3 (the plan), M4 (faults),
M5 (inspections), M6 (the portal) and the visit signature
(`2026-10-22-visit-signature-design.md`).

Written with defaults, at the user's request ("detailed"): they will review
it before anything is built. Every decision below is a default unless it says
otherwise.

## Why

A building manager pays for maintenance every month and sees almost none of
it. The technician comes while they are at work, the paper form stays in the
machine room, and the first thing they hear about the lifts is a resident's
complaint. The portal (M6) answers questions when somebody thinks to ask
them. Nobody sits down on the first of the month and opens it.

The report closes that gap from the firm's side. On the first of every month,
each customer gets one document for the month before. It shows what was done
on each of their lifts, who signed for it, what broke, what the inspector
found and what is coming next. It is the paper the manager takes to the
residents' meeting, files for the building's records, and forwards to the
board when they ask what they pay for.

For the firm it is the cheapest retention tool there is. A customer who gets
a clear monthly account of the work is a customer who renews.

## Goals

- **One PDF per customer per month,** generated automatically after the month
  ends, with the same facts the office and the portal already show. Nothing
  is typed by hand.
- **Sent by e-mail** to the customer's contacts the firm has marked to receive
  it, with the PDF attached. It can also be downloaded later from the office
  and from the portal.
- **Honest about a bad month:** a lift that missed its visit, a refused
  signature or an open correction deadline appears as it is. The report
  never smooths anything over.
- **Nothing reaches a customer the firm hasn't switched on.** The firm turns
  the e-mails on. Until then the reports are generated and kept, so the
  office can read them first.
- **Readable on a phone,** because that is where the e-mail is opened, and
  printable on A4.

## Non-goals

- Editing a report before it goes out, or writing a covering note. The report
  is generated from the record. A wrong report is fixed by fixing the record
  and generating it again (see "Regenerate").
- Per-lift reports, weekly reports, or reports for a range of dates.
- Branding beyond the firm's name and logo, which already exist: no
  colours, templates or fonts per firm.
- Languages other than Turkish.
- Sending to residents, SMS, or WhatsApp.
- Invoices or amounts. Billing (M7) is the accountant's and goes out on its
  own path. The report says what was done, not what it cost.
- A legal or e-signed document.

## Anti-goals (it fails if…)

- …a customer receives a report the firm didn't intend to send. The firm's
  switch is off until turned on, and recipients are chosen, never guessed.
- …a report says a visit happened that didn't, or hides one that was missed.
- …one customer's report contains anything of another customer's.
- …the same month's e-mail reaches the same person twice because a job ran
  twice.
- …a report fails silently: the office can always see what was sent, to whom,
  and what wasn't, and why.

## Who receives it

- **The firm's switch:** `Company.monthly_reports_enabled`, off by default.
  Only the owner can turn it on, in company settings. While it is off,
  reports are generated and stored, and nothing is e-mailed.
- **The recipients:** `CustomerContact.receives_monthly_report`, a new
  boolean, off by default. The office ticks it per contact on the customer
  page. A contact without an e-mail address can't be ticked.
- **Migration default:** for each customer, the **primary** contact with an
  e-mail is ticked, so turning the switch on reaches somebody sensible
  without a hundred clicks. Nothing is sent until the switch is on.
- A customer with nobody ticked still gets the report generated. It is marked
  "alıcı yok" (no recipient) in the office, so the gap is visible.

## Which customers, which month

- A report is made for **every customer with at least one lift under an
  `ACTIVE` contract on any day of the month.** Lifts that left the contract
  mid-month are covered for the days they were in it.
- **The period is the calendar month just ended** (`YYYY-MM`), by the
  Istanbul calendar.
- One report per `(customer, period)`, unique.

## What the report contains

A4 portrait, Turkish, rendered with WeasyPrint like the invoice drafts. Light
on ink: black text, thin rules, and one accent colour for the headings. Each
section is skipped when it has nothing to say, except where noted.

1. **Header:** the firm's logo and name, the customer's name, "Ekim 2026
   Bakım Raporu", and the generation date.
2. **Summary** (always shown):
   - lifts covered;
   - visits planned vs. completed, as "11 / 12";
   - visits by status (completed, incomplete, access denied);
   - signatures: signed / nobody there / declined;
   - faults opened, resolved, and still open at month end;
   - emergencies (somebody trapped), with the average response time;
   - open correction deadlines at month end.
   - **One sentence that leads**, picked by rule, not written by anyone. For
     example "Tüm asansörlerin bakımı yapıldı" ("every lift was maintained"),
     or "2 asansörün bakımı bu ay yapılamadı" ("2 lifts weren't maintained
     this month"). See "Lead sentence" below.
3. **Per lift**, in route order (district, neighbourhood, building, lift):
   - its name, building and registration number;
   - **visits:** date, technician's first name, status, the checklist items
     marked "attention needed" with their notes, and parts replaced;
   - the signature: the signer's name and role, the time, and the signature
     image as a small thumbnail. Or "Binada kimse yoktu" / "İmzalamayı
     reddetti". "İmzadan sonra düzenlendi" is shown when the visit was
     amended after signing;
   - **a missed visit**, said as a line: "Bu ay bakım yapılmadı — son bakım
     08.09.2026" ("not maintained this month — last visit 08.09.2026"),
     whenever the lift had a planned row for the month that ended
     unfinished;
   - **faults** reported this month: reference, date, what was reported
     (description, or "Kabinde mahsur kalan" for trapped), the status at
     month end, and the response time (reported → on site) when known;
   - **inspections** this month: the label, the body and the date, the
     non-conformities and their status, and the correction deadline if
     still open.
4. **Coming next** (always shown, even if empty):
   - each lift's next maintenance due date;
   - the next periodic inspection when it is within 60 days;
   - open correction deadlines with the days left.
5. **Footer:** the firm's phone and e-mail, the portal address when the
   customer has portal access, and "Bu rapor ShiftLush kayıtlarından
   otomatik olarak hazırlanmıştır." ("This report is generated automatically
   from ShiftLush records.").

**Lead sentence**, in order of precedence, the first that applies:

1. An open correction deadline has passed: "{n} asansörde düzeltme süresi
   geçti." ("the correction deadline has passed on n lifts")
2. Some lift was not maintained: "{n} asansörün bakımı bu ay yapılamadı."
3. There were emergencies: "Bu ay {n} acil arıza oldu; ortalama müdahale
   süresi {m} dakika." ("n emergencies this month; average response m
   minutes")
4. Otherwise: "Tüm asansörlerin bakımı yapıldı."

**Size:** signature thumbnails are fetched from storage at render time. A
thumbnail that can't be fetched is replaced by the signer's name alone, and
never fails the report. A typical customer (5 lifts, 5 visits) should come
out under 500 KB.

## The record

`CustomerReport` (`apps.reports`, a new app):

| Field | Meaning |
|---|---|
| `company`, `customer` | Tenant-scoped. |
| `period` | `"YYYY-MM"`. Unique with `customer` while not deleted. |
| `file` | FK to an `Attachment`, category `monthly_report`, object type `customer`. |
| `generated_at` | When the PDF was made. |
| `summary` | JSON with the summary numbers and the lead sentence's key, so lists can show them without opening the PDF. |
| `delivery` | `pending`, `sent`, `not_enabled` (firm switch off), `no_recipient` or `failed`. |
| `sent_at` | When the e-mail went out. |
| `recipients` | The e-mail addresses it was sent to, as they were then. |
| `error` | The failure's reason for the office, empty otherwise. |

The PDF is kept like any attachment. Reports are never deleted by the
product.

## The job

- **`monthly_customer_reports`**, daily at 08:00 on the M0 scheduler.
- **Day 1:** it generates the previous month's reports.
- **Any later day:** it generates whatever is missing for the previous month,
  so a missed run catches up. It never e-mails a report whose month ended
  more than 7 days ago; that one is marked `not_enabled`/`failed` for the
  office to send by hand.
- **Per customer, in its own transaction,** so one failure doesn't stop the
  rest:
  1. render the PDF;
  2. store it as an attachment;
  3. create the `CustomerReport`;
  4. if the firm's switch is on and there are recipients, queue the e-mail
     after commit.
- **Idempotent** through the unique `(customer, period)`. A second run makes
  nothing and sends nothing.
- **The e-mail** is one message per recipient, with the PDF attached. Its
  subject is "{Firma} — Ekim 2026 bakım raporu ({Müşteri})". The body gives
  the lead sentence and three summary numbers. A recipient with an active
  portal account also gets a link to the portal's reports. A send failure
  marks the report `failed` with its reason. It isn't retried automatically.
- **The office is told:** one in-app notification per firm per run, a new
  kind `monthly_reports_ready`: "Ekim 2026 raporları hazır: 14 gönderildi, 2
  alıcısız." ("October 2026 reports are ready: 14 sent, 2 without a
  recipient.") It goes to the owner and admin, with the usual e-mail
  preference.

## API

| | |
|---|---|
| `GET /customer-reports/` | Filters: `customer`, `period`, `delivery`. For the office roles (owner, admin, operations) and accountant read. Not technicians. |
| `GET /customer-reports/{id}/` | One report, with its summary, delivery state and file id. The file downloads through the attachments' `download-url`. |
| `POST /customer-reports/{id}/send/` | Office roles. Sends (or re-sends) to the current recipients now, whatever the switch and the 7-day rule say. A person chose to send it. Needs an `Idempotency-Key`. Records `sent_at` and `recipients`. |
| `POST /customer-reports/preview/` | `{customer, period}`. Office roles. Renders the PDF for any period, including the current month to date, **without storing it**. It returns the PDF bytes, for "how would this look". |
| `POST /customer-reports/{id}/regenerate/` | Owner and admin. Renders the PDF again from the current record and replaces the file, keeping the old attachment soft-deleted for the audit trail. It doesn't send. |
| `GET /portal/reports/` | The contact's own customer's reports, newest first. The PDF downloads through the portal files endpoint, which allows category `monthly_report` for the contact's customer. |
| Company | `monthly_reports_enabled`: read for every role, write for the owner only. |
| `CustomerContact` | `receives_monthly_report`. It is refused with an e-mail-less contact (`400`, `MONTHLY_REPORT_NEEDS_EMAIL`). |

New error codes: `MONTHLY_REPORT_NEEDS_EMAIL`, `REPORT_NO_RECIPIENT` (send with
nobody ticked).

## Web

- **Company settings:** a "Aylık müşteri raporları" switch (owner only) with
  one line on what it does. When the switch is turned on, a confirmation
  dialog says how many customers and contacts will receive e-mail next month.
- **Customer detail:** a "Aylık raporlar" panel.
  - It lists the reports newest first, each with its period, the lead
    sentence, its delivery state as a chip (Gönderildi / Alıcı yok /
    Gönderim kapalı / Başarısız), "İndir", and "Gönder" (or "Yeniden
    gönder").
  - Above the list is "Bu ayın önizlemesi", which opens the to-date PDF.
- **Customer contacts:** a "Aylık raporu alır" checkbox per contact. It is
  disabled with a hint when the contact has no e-mail.
- **A "Raporlar" screen** in the office nav lists every customer's report for
  a chosen month, with the delivery filter. "Alıcı yok" and "Başarısız" are
  what the office acts on.
- **Portal:** a "Raporlar" tab with the customer's reports and "İndir".
- **Notifications:** the bell shows `monthly_reports_ready` and links to the
  Raporlar screen for that month.

## Demo

The demo firm's switch stays **off**, so nothing goes to an inbox. Refresh
generates the previous month's reports for the demo customers, with the files
stored when the bucket is reachable (the same rule as the signatures). The
Raporlar screen and the portal then show real PDFs. Each demo customer's
primary contact is ticked.

## Delivery

1. **API, part 1.** The `reports` app and model, the PDF template and
   renderer, the switch and contact flag (migrations), the job, the e-mail
   with an attachment (`core.mail` gains attachments), the notification
   kind, the endpoints and error codes, and the demo. OpenAPI is updated.
2. **Web.** The settings switch, the contact checkbox, the customer panel,
   the Raporlar screen, the portal tab, the notification, and the
   translations.

The PDF template is the biggest single piece. It gets a fixture-driven test
that renders a full report and checks the text of every section (via
WeasyPrint's text extraction, or by testing `build_html`), plus a real render
in CI where WeasyPrint is installed.

## Acceptance

- [ ] On the 1st, every customer with a covered lift last month has one
      report for that month. A second run adds none.
- [ ] With the switch off, nothing is e-mailed, and every report reads
      "Gönderim kapalı".
- [ ] With the switch on, each ticked contact gets one e-mail with the PDF
      attached. A customer with nobody ticked shows "Alıcı yok".
- [ ] A lift that missed its visit is said as missed, the lead sentence
      reflects it, and a refused signature shows as refused.
- [ ] A report never contains another customer's lift, visit, fault or
      inspection. A test builds two customers and checks it.
- [ ] Preview renders the current month to date without storing anything.
- [ ] The portal lists and downloads its own customer's reports only.
- [ ] The office is told once per run what was sent and what wasn't.
- [ ] Backend `make lint`, `make mypy`, `make check` and `make test` pass.
      Web `npm run verify` passes, and smoke renders the panel, the Raporlar
      screen and the portal tab.

## Calls made without the user

- The firm's switch is off by default, and only the owner turns it on.
- Recipients are chosen per contact. The primary contact with an e-mail is
  ticked by the migration.
- The report is sent on the 1st at 08:00, for the previous calendar month. A
  missed run catches up for 7 days and then stops e-mailing.
- The PDF is attached to the e-mail. It is also downloadable in the office
  and the portal.
- One lead sentence chosen by rule, with no hand-written covering note.
- No money in the report.
- Signature thumbnails are included. If one can't be fetched, the name alone
  is shown.
- No automatic retry of a failed send. The office sees it and presses
  "Gönder".
- Regenerating replaces the file but keeps the old one in the audit trail.
- The demo firm's switch stays off.

## Left to the builder

- The PDF's exact typography and layout within "light on ink".
- Whether the summary numbers are computed in SQL or in Python. At this scale
  either works.
- How thumbnails are embedded: data URIs, or WeasyPrint's URL fetcher.
- Whether `apps.reports` holds the e-mail template or `templates/email`
  does. Follow the existing layout.
