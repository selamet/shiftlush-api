# M2 — Notifications and the contract lifecycle

Date: 2026-10-20. Roadmap: M2 (`2026-10-20-phase-2-roadmap.md`, #87).
Runs on the background jobs from M0 (`2026-10-20-background-jobs-design.md`).

## Why

A firm's revenue sits in its contracts, and nothing in the product watches them.

- **Nobody is warned before a contract ends.** `renewal_notice_days` (default
  60) is stored on every contract and read by nothing.
- **Nothing happens when one ends.** An `ACTIVE` contract stays `ACTIVE` past
  its `end_date`, and it keeps holding its elevators.
- **`auto_renew` is stored and read by nothing.**
- **An overdue elevator is invisible.** An elevator whose
  `next_maintenance_date` has passed (M1 keeps that date) stays that way until
  somebody happens to open it.

M2 adds the means to tell people (notifications) and the first three things
worth telling them.

## Notifications

### Model — `apps.notifications`

`Notification`:

| Field | Meaning |
|---|---|
| `company`, `recipient` | Tenant-scoped. Only the recipient ever sees it. |
| `kind` | An enum; the list is below. |
| `subject_type`, `subject_id` | What it is about, e.g. `contract` and an id, so the client can link to it. |
| `data` | JSON with the facts the sentence needs (`contract_number`, `end_date`, `elevator_name`, …), captured when it was raised. |
| `read_at`, `created_at` | |
| `dedupe_key` | Unique per recipient, so a job that runs again raises nothing new. |

- **No sentences in the API.** The project rule (`core/mail.py`): the client
  renders the Turkish from `kind` and `data`, and e-mail templates hold their
  Turkish literally.
- `NotificationPreference(user, kind, email: bool)`.
  - In-app is always on.
  - E-mail defaults to **on** for the contract kinds and **off** for
    `maintenance_overdue`, which is daily operational noise for most readers.

### Delivery

`notify(kind, recipients, subject, data, dedupe_key)`:

1. Creates one notification per recipient.
   - It skips anyone who already has that `dedupe_key`, using the unique
     constraint rather than a read-then-write.
   - It skips inactive users.
2. For each one created, if the recipient's preference allows e-mail,
   enqueues `send_notification_email(id)`. That is a `django.tasks` task on
   the M0 worker, so a slow mail server never holds a job or a request.

The e-mail is one shared template set, `templates/email/notification/`, on the
existing base layout, with a link to the subject in the web app. Each kind adds
two partials under `kinds/` (subject and body sentence), so adding a kind means
adding files and nothing else. The `data` keys per kind are listed on
`NotificationKind`.

### API

| | |
|---|---|
| `GET /notifications/` | The signed-in user's own, newest first, paginated; `?unread=true`. |
| `GET /notifications/unread-count/` | `{count}`, for the bell. |
| `POST /notifications/{id}/read/` | Idempotent. |
| `POST /notifications/read-all/` | |
| `GET /notifications/preferences/` | Every kind with its `email` flag. |
| `PUT /notifications/preferences/` | Replaces the flags. |

Every role may use these for its own notifications. Nobody can read anyone
else's.

## The jobs (daily, Istanbul time)

All three run on the M0 scheduler and are idempotent by `dedupe_key`. "Today"
is the Istanbul date (`core.scheduling.LOCAL_TZ`).

### 06:00 — `contract_renewal_notice`

- **Picks:** every `ACTIVE` contract whose `end_date - renewal_notice_days <=
  today < end_date`.
- **Recipients:** owner and admin.
- **Kind:** `contract_renewal_due`, with `{contract_number, customer_name,
  end_date, auto_renew}`.
- **Dedupe key:** `(contract, end_date)`. One notice per term; a renewal with a
  new end date gets its own.

### 00:30 — `contract_expiry`

- **Picks:** every `ACTIVE` contract whose `end_date < today`.
- **When `auto_renew` is set:**
  - It is renewed through the existing `renew()`: same terms and elevators, the
    new term starting the day after the old one ends, the same length.
  - The successor is then **activated**. Auto-renewal means the terms carry on,
    and a draft successor would make every one of its elevators refuse
    maintenance visits (they need an `ACTIVE` contract) until somebody noticed.
  - The exception is a contract whose `vat_rate` is unset. Its successor stays
    a draft, because activating it would bill an invented rate. That raises
    `contract_auto_renewal_blocked`.
  - Otherwise it raises `contract_auto_renewed`, with the successor's number.
- **Otherwise:**
  - The contract becomes `EXPIRED`.
  - Its open lines close on `end_date`, and its elevators fall back to
    `uncontracted`. This is the same rule as termination and renewal: a
    finished contract holds no elevators, or the partial unique index blocks
    them forever.
  - It raises `contract_expired`.
- **Recipients:** owner and admin.
- **Where the code lives:** a new `expire()` service in
  `apps/contracts/services.py`, beside `terminate()` and `renew()`. It is
  atomic per contract, so one bad contract does not stop the rest.

### 07:00 — `maintenance_overdue`

- **Picks:** every elevator whose `next_maintenance_date < today` that is
  covered by an `ACTIVE` contract.
- **Recipients:** owner, admin, operations, and the technicians assigned to
  the elevator's customer.
- **Kind:** `maintenance_overdue`, with `{elevator_name, building_name,
  next_maintenance_date}`.
- **Dedupe key:** `(elevator, next_maintenance_date)`. Once per missed due
  date, not once a day; recording a visit moves the date, and a new miss is a
  new notice.

## Web (separate PR)

- A bell in the top bar with the unread count, polled every 60 s while visible.
- A notification panel and a `/notifications` screen. Each row renders a
  Turkish sentence from `kind` and `data`, links to its subject, and shows
  unread rows distinctly, with "Tümünü okundu say".
- Settings gains a section of per-kind e-mail checkboxes.

## Non-goals

- SMS and push notifications.
- Notifications for M3–M7 events. Each module adds its own kinds.
- Digest e-mails.

## Decisions made without the user

- **An auto-renewed successor is activated**, unless VAT is unset. Without
  that, auto-renewal would block visits.
- **An expired contract releases its elevators.** This is consistent with
  `terminate()` and `renew()`.
- **Default e-mail settings:** on for contract events, off for overdue
  maintenance.
- **Run times:** 00:30, 06:00 and 07:00 Istanbul.
- **Overdue recipients** include the assigned technicians.

## Slices

1. **Backend:** the notifications app, API, delivery and e-mail templates.
2. **Backend:** the three jobs and `expire()`.
3. **Web:** the bell, the list and the preferences.
