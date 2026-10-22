# Calendar planning and route order

Date: 2026-10-22. Phase 3, first milestone. Builds on M3
(`2026-10-20-m3-maintenance-planning-design.md`): the `PlannedVisit` row per
lift per month, with `due_date`, `planned_date`, `technician` and
`manually_edited`.

Written with defaults, at the user's request: they will review it before
anything is built. Every decision below is a default unless it says otherwise.

## Why

M3 gives the office a month's plan as a table. The table answers "which lifts
are due, and who has them". It doesn't answer "what does Ahmet's Tuesday
look like", and moving a visit from Tuesday to Thursday means finding the row,
opening the inline editor and typing a date. Planning a week is a matter of
days and people, and the table shows neither.

The technician's "Bugün" list is in route order, but route order is the
address hierarchy (district, neighbourhood, building). Two buildings in
neighbouring districts can be 500 m apart and still sit at opposite ends of
the list.

This milestone adds two things, in this order:

- **A. The calendar.** The office plans the week by dragging visits between
  days and technicians.
- **B. Route order.** A day's stops are ordered by distance, from the
  buildings' coordinates, with no external service.

## Goals

- The office plans a week for every technician on one screen, and moving a
  visit is one drag.
- Nothing the calendar allows breaks an M3 rule: one row per lift per month,
  the planned day inside the row's month, and edited rows that the generator
  never touches.
- A technician's day is ordered so they drive less. Lifts with no position
  still appear, at the end, and the office can see which lifts these are.
- It works in the demo tenant on the day it ships.

## Non-goals

- Road distances, travel times, traffic and time windows. Straight-line
  distance only, labelled "kuş uçuşu".
- Automatic assignment or multi-day optimisation ("spread these 200 lifts
  over the month"). The office decides who goes on which day. B only orders a
  day that is already decided.
- A calendar for technicians. They keep "Bugün" and "Bu hafta".
- Public holidays, leave and technician capacity limits.
- A stored or hand-edited route order. The order is computed on read.
- Planning fault visits. They stay M4's.

## Anti-goals (it fails if…)

- …a drag that the server refused still looks applied.
- …dragging ten visits to one technician sends them ten e-mails.
- …the calendar works only with a mouse. Keyboard users need the same
  result, and so does a touch screen on a tablet.
- …a lift with no coordinates silently drops out of the day.

## A. The calendar

### Screen (web, office roles: owner, admin, operations)

`/plan` gains a view switch: **Liste** (the M3 table, unchanged) and
**Takvim**. The choice is kept in the URL (`?view=calendar`) like every other
list setting.

**Week view (the default).**

- Columns are days, Monday to Sunday. Sunday is shaded but you can still drop
  visits on it.
- Rows are the firm's active technicians, plus an **Atanmamış** row at the top.
- Each cell lists that technician's visits planned for that day as compact
  cards. A card shows the lift and building name, and the overdue (with its
  days) and "tried, not done" flags. It uses the same wording as
  `PlanRowFlags` and the same row stripe.
- Each cell shows a count. A cell with more than 8 visits shows its count in
  the warning tone. This is a hint, not a limit.
- A side tray, **Günü belirsiz**, lists the planned rows with no
  `planned_date` for the periods the week touches. Overdue rows come first,
  then rows by due date. The tray can be filtered by technician.
- Week navigation: previous, next and "Bu hafta". A week can span two months.
  Each card belongs to its own period, and days outside a card's period
  are shaded while that card is being dragged.
- Done and cancelled rows show as muted cards that can't be dragged, so the
  week reads true.

**Month view.** This view is read-only and shows how much work there is. It is
a grid of days, and each day shows a count per technician (initials and a
number). Clicking a day opens the week that contains it.

**Drag and drop.**

| Drop | Effect |
|---|---|
| Onto another day, same technician row | `planned_date` = that day |
| Onto another technician row | `technician` = that person (and the day if it's a different column) |
| Onto the **Atanmamış** row | `technician` = null |
| Onto the tray | `planned_date` = null |

- One `PATCH /planned-visits/{id}/` per drop, applied optimistically. If the
  server refuses, the card goes back to where it was and the server's message
  is shown inline. It isn't a toast that can be missed.
- Multi-select drag is not in this version. Bulk assignment stays on the list
  view.
- **Keyboard and touch.** Every card has a "Taşı…" action that opens a small
  dialog with a day picker (limited to the card's period) and a technician
  picker. It works with the keyboard and with a screen reader, and it is the
  fallback wherever dragging is awkward. The drag library must support
  keyboard and touch sensors.
- Below the `md` breakpoint the calendar isn't offered. `/plan` stays the
  list there.

**Library.** `@dnd-kit/core` (and `@dnd-kit/utilities` if needed), pinned to
an exact version. It is chosen for its keyboard and touch sensors and
accessible announcements. No full calendar component: the grid is plain
markup on the design system.

### API changes

1. **Server-side rules on `PATCH /planned-visits/{id}/`.** Today only the web
   date picker enforces them.
   - `planned_date` must fall inside the row's `period`. Otherwise `400` with
     a new error code `planned_date_outside_period`.
   - A `planned_date` before today (Istanbul) is refused with
     `planned_date_in_past`, unless it is unchanged.
   - A row that is `done` or `cancelled` can't be edited: `409`,
     `plan_row_closed`. If this is already enforced, keep it and add the test.
   - Every new code is translated in the web's error-code table, which the
     `lint:errors` check enforces.
2. **Filters on `GET /planned-visits/`:** `planned_from` and `planned_to`
   (inclusive dates), and `unscheduled=true` (no `planned_date`). The week view
   asks for the week's two periods at most, with these filters, at
   `MAX_PAGE_SIZE`, and follows `next` if there is one.
3. **Fewer assignment e-mails.** When a `PATCH` assigns a row to a
   technician who has an **unread** `visit_assigned` for the same period
   created in the last 15 minutes, that notification is updated in place
   (count and text) instead of a new one being created. No second e-mail is
   sent. Bulk assignment keeps its one-notification-per-action rule. The
   builder chooses how this fits the `dedupe_key` model, as long as both
   behaviours are tested.

## B. Route order

### Where positions come from

- A stop's position is its building's `latitude`/`longitude`, or its
  complex's if the building has none.
- **The firm's starting point.** `Company` gains optional `latitude` and
  `longitude`, which are set on the company settings page with the existing
  `LocationPicker`. When they are set, a day's route starts there. When they
  are not set, the route starts from whichever stop gives the shortest open
  path.

### The algorithm (`apps.maintenance.routing`, pure functions)

- Distances are haversine distances in kilometres.
- The order is built by nearest neighbour from the start, then improved by
  2-opt until no swap helps, on an **open** path (no return to the start).
- Without a firm starting point, it tries every stop as the start (n is a
  day's stops, rarely above 15) and keeps the shortest result.
- The result is deterministic: ties are broken by the M3 route order. The
  same input always gives the same order.
- If the M3 address order of the same stops is shorter, it is returned
  instead, so the suggestion is never worse than what exists today.
- Stops without a position go after the ordered ones, in M3 route order.
- It returns the ordered row ids, the total straight-line km, and the ids of
  rows without a position.

### API

`GET /planned-visits/route/?technician=<id|me>&date=YYYY-MM-DD` returns the
day's ordered stops. A day's stops are the technician's planned rows with
that `planned_date`, plus, for **today only**, their rows with no planned
date that are overdue. Those are what "Bugün" shows today; the builder
aligns this with `lib/plan.isDueToday` so the two can't disagree. Each stop
carries the same fields as a list row, plus its position and its distance
from the previous stop. Technicians can read only their own route. Office
roles can read anyone's.

`GET /buildings/` gains `has_location=true|false`.

### Web

- **Technician "Bugün".** Ordered by the route endpoint. A small header says
  "Önerilen sıra · kuş uçuşu ~N km". Each stop with a position has a
  "Yol tarifi" link (a maps directions URL with the coordinates, which opens
  the phone's maps app). Stops without a position sit under "Konumu
  bilinmeyen" at the end.
- **Office calendar.** Clicking a day's header in a technician's row opens a
  side panel with that day's route: a numbered list and a Leaflet map with
  numbered markers and straight connecting lines. The panel is read-only.
- **Missing positions.** The calendar shows "N asansörün binasında konum
  yok" when any lift with a planned row this period has no position. It links
  to the building list filtered by `has_location=false`.

### Demo

The demo seed gives every demo building a plausible position. The
coordinates are jittered around the centre of the building's district, so
nearby buildings cluster. The demo company also gets a starting point.
`refresh_demo_data` fills positions for demo buildings that have none, and
touches only the demo tenant (SL-161 rules). Without this, B is invisible in
the demo.

## Delivery

Four PRs, in order, each with its own issue:

1. **API, part A.** The PATCH rules, the new filters and coalesced assignment
   notifications. OpenAPI updated.
2. **Web, part A.** The view switch, week and month views, drag and drop,
   "Taşı…", and translations.
3. **API, part B.** Company coordinates (migration), `routing` with unit
   tests, the route endpoint, `has_location`, and the demo positions.
4. **Web, part B.** The "Bugün" order, the day panel with the map, and the
   missing-positions notice.

The M3 spec gets a pointer to this document. The roadmap gets a "Phase 3"
section when part A ships.

## Acceptance

- [ ] Dragging a card to another day or technician changes exactly that
      row's `planned_date` / `technician` and sets `manually_edited`.
      Reloading shows the same.
- [ ] A drop outside the row's month, onto a past day or of a done row is
      refused by the server, the card returns, and the reason is visible.
- [ ] Everything a drag does can be done with the keyboard through "Taşı…".
- [ ] Assigning ten rows one by one to the same technician within 15 minutes
      gives them one notification and at most one e-mail.
- [ ] For a fixed set of coordinates, the route order is the same on every
      run, and it is never longer than the M3 address order.
- [ ] A day with stops that have no position lists them last, and the office
      sees how many lifts lack a position.
- [ ] In the demo tenant, the calendar has cards on this week's days and a
      technician's "Bugün" shows an ordered route with a km total.
- [ ] Backend `make lint`, `make mypy`, `make check` and `make test` pass.
      Web `npm run verify` passes, and smoke renders the calendar for the
      office roles.

## Calls made without the user

- Week view as the default, with technicians as rows. The month view only
  shows load.
- Office only. Technicians get no calendar.
- One PATCH per drop. No multi-drag.
- Sunday allowed but shaded. Holidays are ignored.
- More than 8 visits a day is a hint, not a limit.
- Planning onto a past day is refused.
- `@dnd-kit/core` for drag and drop.
- Straight-line distance, nearest neighbour plus 2-opt, an open path, and a
  firm starting point that is optional.
- The route is computed on read and never stored. The office can't reorder
  it.
- Assignment e-mails coalesce within 15 minutes per technician and period.

## Left to the builder

- Card density and exact layout inside the design system.
- How coalescing fits the notification model.
- Whether the route endpoint pre-computes anything; it doesn't need to at
  this scale.
- The 2-opt iteration cap, which is only a guard. With n ≤ 30 it converges
  quickly.
