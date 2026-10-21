# M3 — Maintenance planning

Date: 2026-10-20. Roadmap: M3 (`2026-10-20-phase-2-roadmap.md`, #88).
Builds on M1 (visits), M2 (notifications) and M0 (the scheduler).

## Why

Every elevator under an active contract needs a visit at least every
`maintenance_interval_days` (at most 30, by regulation). Today the product
records a visit once it has happened. Nothing says, at the start of the month,
which lifts are due, who is going and in what order. The office plans on paper
or in a spreadsheet, and the technician finds out from a phone call.

M3 turns the obligation into a month's plan: one row per lift, with a due date
and a technician. The office can rebalance it, the technician works through it
in route order, and a recorded visit ticks it off.

## The plan

### `PlannedVisit` (`apps.maintenance`)

| Field | Meaning |
|---|---|
| `company`, `elevator` | Tenant-scoped. |
| `period` | `"YYYY-MM"`, the month the visit belongs to. |
| `due_date` | When it falls due (see below). |
| `planned_date` | Nullable. The day the office or technician means to go, if they have said. |
| `technician` | Nullable, scoped to the firm like the visit's own field (SL-124). |
| `status` | `planned`, `done` or `cancelled`. |
| `visit` | Nullable FK to the `MaintenanceVisit` that closed it. |
| `manually_edited` | Set by any PATCH. The generator never overwrites a row a person touched. |

- Unique (`elevator`, `period`) with `is_deleted = false`: one planned visit
  per lift per month. A 30-day maximum interval makes one per month the
  obligation.
- Ordering ends in the primary key (the structural test requires it).

### Generation — a daily job, `plan_maintenance` (01:00 Istanbul)

The job runs daily on the M0 scheduler and is idempotent through the unique
constraint. It makes sure **this month and next month** have a row for every
lift covered by an `ACTIVE` contract:

- **Due date.** The lift's `next_maintenance_date` (M1), as it is. A date
  before the month is kept: an October row can be due on 8 September, and that
  is the overdue fact (amended by SL-162; it used to be clamped to the 1st,
  which printed "01.10" on every carried-over row). If the lift has never been
  visited, the 15th. If its next date is already past the month (a visit early
  in the month moved it), the month's last day.
- **The row follows the lift.** Whenever the lift's `next_maintenance_date` is
  recalculated, its unedited planned rows for this month and later take the new
  due date (SL-162), and the daily job does the same. The read side carries
  `days_overdue`.
- **Default technician.** If exactly one active technician is assigned to the
  lift's customer, that person. Otherwise unassigned: the office decides, and
  the plan screen shows the unassigned rows first.
- **Rows for lifts that lost cover.** A `planned` row whose lift no longer has
  an `ACTIVE` contract becomes `cancelled`, unless it was manually edited. A
  manual edit is left for a person to resolve, and is flagged on the screen.
- **Notifications.** A new `visit_assigned` notification goes to a technician
  when rows are assigned to them. That covers generation, PATCH and bulk
  assignment. It is one notification per assignment action, not per row
  ("12 bakım size atandı — Ekim 2026"), deduped per (technician, period,
  action id).

### Closing a row

Recording a `MaintenanceVisit` (M1) links it to that lift's open row for the
visit's month:

- **`completed`:** the row becomes `done`, with `visit` set.
- **`incomplete` or `access_denied`:** the row stays `planned`, and the visit
  is linked as the latest attempt, so the plan shows "tried, not done".
- **No row for that month** (an ad-hoc visit, or a lift planned in another
  month): nothing to close. The visit stands on its own.
- **Deleting a visit** reopens the row it closed. Editing a visit's date into
  another month moves the link.

This lives in `apps.maintenance.services`, in the same transaction as the
visit's create, update and delete, so the plan and the record cannot disagree.

### Overdue

M2's `maintenance_overdue` keys on `next_maintenance_date`, which already
means "no completed visit within the interval". The plan does not change that
fact, so the job stays as it is. The plan screen shows overdue rows (`planned`
with `due_date < today`) on its own.

## API

| | |
|---|---|
| `GET /planned-visits/` | Filters: `period` (required), `technician` (an id, or `me`), `unassigned=true`, `status`, `due_before`, `district`, `customer`. Each row carries the elevator name, building, neighbourhood and district names, customer name, and the latest attempt's status, so the screen needs no second request per row. Ordered by district, neighbourhood, building, then elevator (route order) by default, with `?ordering=due_date` available. |
| `PATCH /planned-visits/{id}/` | `technician` and `planned_date`. Office roles only (owner, admin, operations). Sets `manually_edited`. |
| `POST /planned-visits/bulk-assign/` | `{ids: [...], technician}`. Office roles only. All or nothing. One `visit_assigned` notification per technician. |
| `GET /planned-visits/summary/?period=` | Counts per technician and status, plus unassigned and overdue, for the plan screen's header. |

- **Technicians** read their own rows (assigned to them) and the unassigned
  rows of the customers they are assigned to, the usual technician narrowing.
  They cannot PATCH.
- **Accountants** have no access, as with visits.

## Web (separate PR)

- **`/plan`** (office): a month picker; the summary header (per technician:
  planned, done, overdue; unassigned; overdue); and a table in route order
  with filters for technician, unassigned, status and district. Rows can be
  multi-selected and bulk-assigned to a technician. The planned date and
  technician can be edited inline. Overdue and "tried, not done" rows are
  distinct, and not by colour alone.
- **Technician, mobile:** "Bugün" and "Bu hafta" lists of their rows in
  route order. Each row opens the elevator, where "Bakım kaydı gir" (M1) is a
  tap away. This becomes the technician's landing screen after login.
- **Elevator detail:** this month's planned row (due date, technician, status).
- Turkish messages for the new `visit_assigned` notification kind.

## Non-goals

- Route optimisation. The order is by district and neighbourhood, not a
  solver.
- Planning more than one visit per lift per month.
- Calendar or drag-and-drop scheduling. The planned date is a field.
- Planning fault visits. They are M4's.

## Decisions made without the user

- One row per lift per month, generated for this month and next.
- Due date: `next_maintenance_date` as it is (held to the month's end if
  later), otherwise the 15th.
- The default technician is the only one assigned to the customer, otherwise
  nobody.
- A manually edited row is never overwritten or auto-cancelled.
- `incomplete` and `access_denied` visits do not close a row.
- The technician's landing screen becomes "Bugün".

## Slices

1. **Backend:** the model, the generator job, closing rows from visits, the
   API, and the `visit_assigned` notification.
2. **Web:** `/plan`, the technician's "Bugün" and "Bu hafta", and the planned
   row on the elevator detail.
