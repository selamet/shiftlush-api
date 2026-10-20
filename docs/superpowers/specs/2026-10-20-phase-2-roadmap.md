# Phase 2 roadmap

Date: 2026-10-20. Source: `faz1-sartname.md`, which ends Phase 1's scope with
the list this document turns into an order of work:

> Bakım planı üretimi, bakım formu/kaydı, arıza yönetimi, periyodik kontrol
> modülü (alanlar şemada olacak, ekran olmayacak), faturalama, mobil uygulama,
> müşteri portalı, bildirim/hatırlatma motoru. (line 137)

No user research went into this. The order follows dependencies, then how
much existing data each module puts to work. A firm using the product should
re-order it.

## Where Phase 1 left things

- Phase 1 is live: `shiftlush-api.selamet.dev` (Django 6.1, PostgreSQL) and
  `shiftlush.selamet.dev` (Vite + React 19, Vercel).
- The first Phase 2 record is in: `apps.maintenance`
  (`MaintenanceVisit`, 16-item checklist,
  `Elevator.last/next_maintenance_date`), backend only — SL-119, #82, #83.
- These were left in the schema so Phase 2 could start without retrofits:
  - `Elevator.maintenance_interval_days`, plus the five inspection fields
    (`last_inspection_date`, `inspection_label`, `next_inspection_date`,
    `inspection_body`, `inspection_report_number`)
  - `Contract.scope`, `billing_period`, `auto_renew`, `renewal_notice_days`,
    `previous_contract`
  - the `ACCOUNTANT` role, and the `inspection_report` attachment category
  - `Idempotency-Key` handling, which spec §8.10 says becomes mandatory for
    maintenance records
- Architecture decided in the spec (§2.5): background work uses
  `django.tasks`, **not Celery**.
- Spec §20: Phase 2 is used **on a phone by technicians**. Every screen built
  here is mobile-first, and works in the dark theme of a machine room.

## Principles carried over

- **A spec, a plan, then the code.** Each milestone below gets its own design
  document (`docs/superpowers/specs/`) and implementation plan
  (`docs/superpowers/plans/`) before any code, the way SL-119 did. This
  roadmap fixes the order and the boundaries, not the field lists.
- **One issue per shippable slice.** Backend and frontend stay separate PRs in
  separate repos, with backend first. The contract is regenerated and
  `CHANGELOG-API.md` is updated in the same PR.
- **Keep the existing gates green.** That means `make lint mypy check test`,
  the N+1 test, `--fail-on-warn` spec generation, and frontend CI's
  error-code-has-a-Turkish-message check.
- **No module "because it'll be needed".** The spec's rule (line 139) still
  applies inside Phase 2.

## Milestones

Sizes are relative: **S** ≈ 1–2 PRs, **M** ≈ 3–5, **L** ≈ 6+.

### M0 — Foundations (S–M)

**Status (2026-10-20): done.**
1. #81 and #93: every ordering ends in the primary key, including a client's
   `?ordering=`, and pytest treats `UnorderedObjectListWarning` as an error.
2. Branch protection is on in both repos: the CI check is required, and the
   branch must be up to date with `main`.
3. Background jobs: `django-tasks-db`, `worker`, `scheduler`
   (`2026-10-20-background-jobs-design.md`). The proof jobs are the two purges
   that were waiting for a scheduler, not M2's contract job.
4. selamet/shiftlush-web#101: contract pulled, and the Turkish message added.
   The `shiftlush-web-SL-62` worktree is still to be checked and removed.


Close out what is already known to be wrong, and lay down the one piece of
infrastructure everything after M1 needs.

1. **Stable ordering everywhere.**
   - Merge #81 (SL-118).
   - Fix #80: the ~17 remaining querysets that can reorder tied rows.
   - Done when the suite passes with
     `-W error::django.core.paginator.UnorderedObjectListWarning`, and CI
     runs it that way, so the 23 failures that flag shows today stay at
     zero.
2. **Branch protection on `main` in both repos** — required status check.
   #77 records a merge on red CI that a required check would have refused.
3. **Background jobs and a clock.**
   - Pick the `django.tasks` backend for production. First confirm what
     Django 6.1 ships; if it has only the immediate/dummy backends, use the
     `django-tasks` database backend. No Redis queue, no Celery.
   - Add a worker process to `docker-compose.prod.yml`.
   - Add a scheduler: a single `run_scheduled_jobs` management command,
     triggered once a minute by a systemd timer or a compose service. Each job
     is idempotent and keyed by its period, so a double run does nothing.
   - Prove it with one real job: the contract lifecycle job in M2.
4. **Frontend contract sync.**
   - Pull the post-SL-119 `openapi/v1.yaml` into `shiftlush-web`.
   - Add the Turkish message for `NO_ACTIVE_CONTRACT_FOR_ELEVATOR`.
   - Delete the stale `shiftlush-web-SL-62` worktree once it is confirmed
     superseded.

### M1 — Maintenance visit on the phone (M)

**Status (2026-10-20): shipped.** Design:
`2026-10-20-m1-maintenance-visit-mobile-design.md`.
- **Backend.**
  - #97: `Idempotency-Key` required on visit create, inline `items`, and an
    atomic create.
  - #98: maintenance dates on the elevator detail, a technician list for the
    visit form, a date filter, and the create response in the contract.
- **Web.**
  - selamet/shiftlush-web#102: the visit form, the visit list and detail, and
    the elevator's maintenance history.
  - selamet/shiftlush-web#103: the offline outbox.
  - selamet/shiftlush-web#104: the PWA shell.
- **Found on the way.**
  - #95: a visit could name a user from another firm.
  - #96: three business rules reached the client as `invalid`.
- **Open:** #99, two outbox edge cases.
- **Not verified yet:** a real end-to-end run on a phone. Smoke render covers
  every route for every role, and the units cover the form and the outbox, but
  nobody has yet scanned a label in a machine room.


The backend exists; this makes it usable.

- **Technician flow:** scan the QR (`ScanScreen` exists) → elevator
  (`ElevatorDetailMobile` exists) → start a visit → 16-item checklist, one
  thumb, large targets → optional photo from the camera → submit.
- **Office flow:** the visit list and visit detail, the visit history on
  elevator detail, and `last/next_maintenance_date` shown on elevator list
  and detail.
- **Idempotency:**
  - `Idempotency-Key` becomes mandatory on `POST /maintenance-visits/`
    (spec §8.10).
  - The client generates the key when the form opens, not on submit, so a
    retry after a dropped connection cannot double-record.
- **PWA shell:**
  - Installable manifest and a service worker that caches the app shell.
  - An **outbox** for a visit submitted with no signal: kept on the device,
    sent when the connection returns, and visible as "waiting to send".
  - This is the whole of the "mobile app" for Phase 2 (see Non-goals).
- **Out of scope:** editing a submitted visit as a technician. The backend
  already refuses it.

### M2 — Notifications and the contract lifecycle (M)

This milestone goes first among the new modules because it runs on data every
firm already has, and because the modules after it publish into it.

- **Notification model:**
  - Every notification is an in-app record with recipient, kind, subject
    object, `read_at` and `created_at`, and may also produce an e-mail.
  - E-mail reuses the transactional templates from SL-54.
  - A per-user preference decides, for each kind, in-app only or in-app plus
    e-mail.
- **Delivery:**
  - A `notify(kind, recipients, obj)` service writes the record and enqueues
    the e-mail as a task.
  - A unique key on (kind, object, period) makes a re-run of a job a no-op.
- **First jobs (daily):**
  - **Renewal notice** — `renewal_notice_days` before `end_date`, notify
    owner and admin.
  - **Expiry** — move `ACTIVE` contracts past `end_date` to `EXPIRED`, or
    renew them when `auto_renew` is set, through the existing renew service.
    Elevators left with no contract get the `uncontracted` status.
  - **Maintenance overdue** — `next_maintenance_date` has passed with no
    completed visit.
- **Web:**
  - A bell with an unread count.
  - A notification list, where each notification links to its object.
  - A preferences section in Settings.
- **Out of scope:** SMS and push notifications. Both are an M8 decision once
  M1's PWA is in use.

### M3 — Maintenance planning (M–L)

Turns "every elevator needs a visit every ≤30 days" into a monthly schedule
someone can follow.

- **Generating the plan:**
  - A monthly job builds `PlannedVisit` rows for each elevator covered by an
    `ACTIVE` contract, due by `maintenance_interval_days`.
  - Each row starts unassigned, or assigned to the technician who holds the
    customer (via `user_customer`).
  - It is regenerable, and never duplicates or overwrites a planned visit
    someone has edited.
- **Matching completed work:** recording a `MaintenanceVisit` closes the
  matching planned visit. If none matches, the visit stands on its own;
  ad-hoc visits are normal.
- **Views:**
  - Office: this month's plan by technician and by district, unassigned
    visits, overdue visits, and bulk reassignment.
  - Technician: today and this week, ordered by neighbourhood. The
    `address` data is already there.
- **Notifications (via M2):** visit assigned, and visit overdue (replacing
  M2's simple overdue rule with plan-aware logic).
- **Design decision to settle in its spec:** whether the planned visit and
  `MaintenanceVisit` stay two tables or become one record with a lifecycle.
  The current guess is two tables: the plan is an intention, the visit is a
  fact.

### M4 — Fault management (L)

What happens when an elevator stops.

- **`Fault` record:**
  - Fields: elevator, reported-by (staff, customer contact, or public QR
    report), channel, description, priority, assigned technician, and
    timestamps for each status.
  - Priority: `emergency` (trapped passenger), `high`, `normal`.
  - Status flow: `open → assigned → on_site → resolved → closed`, plus
    `cancelled`.
  - Response and resolution times derive from the timestamps.
- **Work record:**
  - What was done, with photos and the parts used (free text plus
    quantity). A parts catalogue is out of scope.
  - Whether the work is **billable**, worked out from `Contract.scope`:
    - `maintenance_only` → repairs are billable
    - `full_coverage` → not billable
    - `maintenance_and_repair` → labour is covered, parts are billable
  - The billable flag can be overridden, and every override is audited.
- **Public report from the QR page:**
  - Anyone at the elevator can report a fault without an account: rate
    limited, captcha-free but throttled per token and per IP, with a
    description and an optional phone number.
  - This is the one unauthenticated write in the product. Its threat model
    goes in the spec.
- **Elevator status:**
  - A fault can set the elevator to `out_of_service`, and resolving the fault
    offers to set it back.
  - An emergency notifies the assigned technician plus owner and admin
    immediately.
- **Web:** a fault board (open by priority and age), fault detail with a
  timeline, and the technician's "my faults" view on the phone.

### M5 — Periodic inspection (M)

The yearly inspection by an accredited inspection body (spec §4.3 glossary:
`inspection`, `inspection_body`, `inspection_label`).

- **`Inspection` record:**
  - Fields: elevator, date, body, report number, label (`green / blue /
    yellow / red`), the report PDF (an `inspection_report` attachment), and
    the next inspection date.
  - The latest record updates the elevator's five inspection fields; they
    become read-only on the elevator form.
- **Non-conformities:**
  - The items the report lists, each with a deadline and a status.
  - An open non-conformity can create a fault work order (M4).
  - Deadlines per label colour come from the inspection regulation. They are
    **to be confirmed against the regulation in M5's spec**, not assumed
    here.
- **Notifications (via M2):** inspection due in N days, a non-conformity
  deadline approaching, and a red label (the elevator becomes `sealed` /
  `out_of_service`).
- **Web:**
  - Inspection history on elevator detail.
  - A firm-wide "inspections due" list.
  - Label colours use the visual language spec §20 requires, kept distinct
    from system status colours.

### M6 — Customer portal (M)

Read access for a customer's contacts, who are not staff users.

- **Login:** the invitation-based login for a `CustomerContact` is a
  separate principal from `User`, with its own token audience.
- **Scope:** a contact sees only their customer's buildings and elevators.
  The tenant-isolation test suite is extended to cover this principal
  before any portal endpoint ships.
- **What they see:** elevators and their status, the maintenance visit
  history with checklist and photos, inspection status and reports, and
  open faults.
- **What they can do:** report a fault (the M4 flow, authenticated this
  time) and download reports.
- **Where it lives:** a separate route tree in `shiftlush-web` with its own
  layout. Not a separate app.

### M7 — Billing (M)

Invoice **drafts**, not invoices.

- **Monthly job:** from each `ACTIVE` contract, by `billing_period`,
  `pricing_type`, `monthly_fee` × covered elevators, and `vat_rate`, plus
  the billable fault work from M4 for the period.
  - A contract whose `vat_status` is `unset` produces no draft. It produces a
    notification instead, the same refusal SL-85 made for contract totals.
- **Draft lifecycle:** `draft → issued → paid / cancelled`. The invoice
  number and date are entered by the accountant, from the firm's own
  e-invoicing system.
- **Exports:** CSV and PDF.
- **Access:** the `ACCOUNTANT` role gets the screens it was created for.

## Non-goals for Phase 2

- **A native mobile app.** The PWA in M1 is the mobile app. Revisit only if
  camera, offline or push needs outgrow it.
- **e-Fatura / e-Arşiv (GİB) integration.** It needs a compliance and
  integrator decision of its own. M7 stops at drafts and exports.
- **A spare-parts catalogue or stock tracking.**
- **Route optimisation.** M3 orders by neighbourhood, not by a solver.
- **SMS and push.** See M2.

## Order and dependencies

```
M0 ──► M1 ──► M3 ──┐
  └──► M2 ─────────┼──► M4 ──► M5 ──► M6 ──► M7
                   │
        (M2 is consumed by M3, M4, M5, M7)
```

- M1 and M2 can run in parallel after M0. They touch different apps and
  different screens.
- M4 before M5, because a non-conformity raises a work order.
- M6 after M4 and M5, so the portal has something to show.
- M7 last, because it reads from contracts (Phase 1) and fault work (M4).

## How each milestone runs

1. Open a GitHub issue for the milestone with the scope above as its
   context. Each slice gets the next `SL-<n>` from the shared counter.
2. Write the design spec and the implementation plan, and commit them on the
   branch.
3. Build the backend PR, then the frontend PR. CI must **pass**, not merely
   finish, before merge.
4. Update this roadmap's milestone with what shipped and what moved.
