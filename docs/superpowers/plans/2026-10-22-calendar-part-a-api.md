# Calendar planning, part A (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the plan's PATCH the rules a drag-and-drop calendar needs, the
filters a week view reads with, and assignment notifications that don't pile
up when an office drags one visit at a time.

**Architecture:** Every change is to the existing `PlannedVisitViewSet`, its
write serializer, its filter set and `apps.maintenance.services`. There is no
new model and no migration. Rules sit in the write serializer, as other field
rules do (`core.exceptions.field_error`). Coalescing sits in the service,
beside `announce_assignments`.

**Tech Stack:** Django 5, DRF, django-filter, drf-spectacular, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-calendar-planning-and-route-order-design.md`
(section "A. The calendar → API changes").

## Global Constraints

- Branch `SL-166` in a worktree at `~/projects/shiftlush-api-SL-166`. Commits
  are titled `SL-166-<type>: …` in English, with no AI attribution lines.
- Error codes are UPPERCASE members of `core.error_codes.ErrorCode`. Field rules
  are raised with `core.exceptions.field_error(field, code)` (400). A conflict
  with the row's state is raised with `core.exceptions.Conflict(code)` (409).
- "Today" is `core.scheduling.local_today()` (Istanbul). Tests pin it with the
  autouse `today` fixture in `tests/test_planned_visits.py` (TODAY = 2026-10-20).
  A new module that reads `local_today` must be patched there too.
- There are no Turkish characters in `apps/`, `core/` or `config/` `.py` files,
  which CI enforces.
- `openapi/v1.yaml` is regenerated with `make spec` and committed.
- Done means `make lint`, `make mypy`, `make check` and `make test` pass.

## Review Focus

1. **A PATCH that resends the row's current past `planned_date` together with a
   technician change.** It must succeed, because the date is unchanged.
   Covered by `test_an_unchanged_past_date_is_not_refused` (Task 1).
2. **A PATCH of `planned_date: null` on a planned row** (dragged to the tray).
   It must succeed and must not trip the past or period rules. Covered by
   `test_clearing_the_date_is_allowed` (Task 1).
3. **Coalescing must not reach across technicians or months.** A second
   assignment to a different technician, or to the same technician in another
   period, raises its own notification. Covered by
   `test_another_period_is_its_own_notification` (Task 3).
4. **A read notification is never edited.** Once the technician has opened it,
   the next assignment is news. Covered by `test_a_read_one_is_not_reused`
   (Task 3).
5. **The `planned_from`/`planned_to` filters with no `planned_date`.** Rows with
   no day must not leak into a date range. Covered by
   `test_a_range_leaves_out_unscheduled_rows` (Task 2).

---

### Task 1: PATCH rules for `planned_date`

**Files:**
- Modify: `core/error_codes.py`. Add three codes to the conflict and
  field-level groups.
- Modify: `apps/maintenance/api/v1/serializers.py`. Add `validate()` to
  `PlannedVisitWriteSerializer`.
- Test: `tests/test_planned_visits.py`. Add a new class `TestEditingRules`.

**Interfaces:**
- Produces: `ErrorCode.PLANNED_DATE_OUTSIDE_PERIOD`,
  `ErrorCode.PLANNED_DATE_IN_PAST` and `ErrorCode.PLAN_ROW_CLOSED`. The web
  part A translates these.

- [ ] **Step 1: Write the failing tests** in `TestEditingRules`. They use the
  `planned` fixture, and each one PATCHes `reverse("planned-visit-detail",
  args=[row.pk])` as `planned.ops`:
  - `test_a_day_outside_the_month_is_refused`: the October row gets
    `{"planned_date": "2026-11-02"}` → 400, and
    `response.data["error"]["details"] == [{"field": "planned_date", "code":
    "PLANNED_DATE_OUTSIDE_PERIOD"}]`. The row's `planned_date` is unchanged.
  - `test_a_past_day_is_refused`: `{"planned_date": "2026-10-19"}` → 400 with
    code `PLANNED_DATE_IN_PAST`.
  - `test_today_is_allowed`: `{"planned_date": "2026-10-20"}` → 200.
  - `test_an_unchanged_past_date_is_not_refused`: set the row's
    `planned_date` to 2026-10-10 directly in the DB, then PATCH
    `{"planned_date": "2026-10-10", "technician": str(planned.tech_b1.id)}`
    → 200.
  - `test_clearing_the_date_is_allowed`: `{"planned_date": None}` → 200, and
    `planned_date` is `None`.
  - `test_a_closed_rows_day_cannot_move`: set the row's `status` to `DONE` in
    the DB, then PATCH `{"planned_date": "2026-10-22"}` → 409, with
    `response.data["error"]["code"] == "PLAN_ROW_CLOSED"`.
  - `test_a_closed_rows_technician_can_be_corrected`: same row, done, PATCH
    `{"technician": str(planned.tech_b1.id)}` → 200.

- [ ] **Step 2: Run the tests to confirm they fail**

  Run: `uv run pytest tests/test_planned_visits.py::TestEditingRules -q`
  Expected: FAIL. The out-of-month, past and closed cases return 200, and the
  codes don't exist yet.

- [ ] **Step 3: Add the codes, then `PlannedVisitWriteSerializer.validate(self,
  attrs: dict[str, Any]) -> dict[str, Any]`.**
  - Do nothing unless `"planned_date" in attrs` and the value differs from
    `self.instance.planned_date`.
  - If the row's status is not `PLANNED`, raise
    `Conflict(ErrorCode.PLAN_ROW_CLOSED)`.
  - If the new value is `None`, accept it.
  - If `period_of(value) != instance.period`, raise
    `field_error("planned_date", ErrorCode.PLANNED_DATE_OUTSIDE_PERIOD)`.
  - If `value < local_today()`, raise
    `field_error("planned_date", ErrorCode.PLANNED_DATE_IN_PAST)`.
  - Import `period_of` from `apps.maintenance.services`. `local_today` is
    already imported in this module and patched by the fixture.

- [ ] **Step 4: Run the tests to confirm they pass**

  Run: `uv run pytest tests/test_planned_visits.py -q`
  Expected: PASS, including the existing `TestEditing`, which edits to
  2026-10-22.

- [ ] **Step 5: Commit**

  `git commit -am "SL-166-feat: the plan refuses a day outside the row's month, in the past, or on a closed row"`

### Task 2: Date-range and unscheduled filters

**Files:**
- Modify: `apps/maintenance/api/v1/views.py`, in `PlannedVisitFilter`.
- Test: `tests/test_planned_visits.py`, in `TestList`.

**Interfaces:**
- Produces: the query parameters `planned_from` and `planned_to` (dates,
  inclusive) and `unscheduled` (boolean). The web week view reads with
  `?period=…&planned_from=…&planned_to=…` and with `?period=…&unscheduled=true`.

- [ ] **Step 1: Write the failing tests.** Use the `planned` fixture with
  three October rows. Give A1 `planned_date` 2026-10-21 and B1 2026-10-27,
  and leave A2 with no day. Each test uses `listed(client, period=OCT, …)`:
  - `test_a_date_range_is_inclusive`:
    `planned_from="2026-10-21", planned_to="2026-10-27"` → A1 and B1.
  - `test_a_range_leaves_out_unscheduled_rows`: `planned_from="2026-10-01"`
    → A2 is not in the result.
  - `test_unscheduled_lists_only_rows_without_a_day`:
    `unscheduled="true"` → only A2.

- [ ] **Step 2: Run them to confirm they fail**

  Run: `uv run pytest tests/test_planned_visits.py::TestList -q`
  Expected: FAIL. The unknown filters are ignored and every row comes back.

- [ ] **Step 3: Add the filters.** `planned_from` and `planned_to` are
  `django_filters.DateFilter(field_name="planned_date", lookup_expr="gte"/"lte")`.
  `unscheduled` is a
  `django_filters.BooleanFilter(field_name="planned_date", lookup_expr="isnull")`.
  Add all three to `Meta.fields`, with a `help_text` on each.

- [ ] **Step 4: Run the tests to confirm they pass**

  Run: `uv run pytest tests/test_planned_visits.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit**

  `git commit -am "SL-166-feat: read the plan by a range of days, or only the rows with no day"`

### Task 3: Coalesce one-at-a-time assignments

**Files:**
- Modify: `apps/maintenance/services.py`. `edit_planned_visit` calls a new
  helper before `announce_assignments`.
- Test: `tests/test_planned_visits.py`. Add a new class `TestAssignmentsCoalesce`.

**Interfaces:**
- Consumes: `announce_assignments(Counter, *, action_id)` (unchanged) and
  `Notification` (`apps.notifications.models`).
- Produces: `coalesce_assignment(technician_id: uuid.UUID, period: str, *,
  now: datetime | None = None) -> bool` in `apps.maintenance.services`. It
  returns `True` when it folded the assignment into an existing notification.
  The constant `ASSIGNMENT_COALESCE_WINDOW = timedelta(minutes=15)`.

- [ ] **Step 1: Write the failing tests.** Each one PATCHes `technician` on
  several October rows as `planned.ops`. B1 and A2 start with no technician
  or with someone else.
  - `test_one_at_a_time_is_one_notification`: assign B1 and then A2 to
    `tech_b1` in two PATCHes → `assigned_notifications(tech_b1)` has one
    notification, whose data is `{"period": OCT, "count": 2}`. Patch
    `send_notification_email.enqueue` (`apps.notifications.services`) and
    assert it was enqueued at most once for `tech_b1`. Run the PATCHes inside
    `django_capture_on_commit_callbacks(execute=True)`.
  - `test_a_read_one_is_not_reused`: after the first PATCH, set `read_at` on
    the notification. The second PATCH gives two notifications.
  - `test_an_old_one_is_not_reused`: after the first PATCH, use `update()` to
    move the notification's `created_at` 16 minutes back. The second PATCH
    gives two notifications.
  - `test_another_period_is_its_own_notification`: assign an October row and
    a November row (NOV, generated by the fixture's job run) to `tech_b1` →
    two notifications, one per period.
  - `test_bulk_assign_still_raises_its_own`: a PATCH, then a bulk-assign of
    another row to the same technician → two notifications, because bulk
    keeps one per action.

- [ ] **Step 2: Run them to confirm they fail**

  Run: `uv run pytest tests/test_planned_visits.py::TestAssignmentsCoalesce -q`
  Expected: FAIL. The first test sees two notifications.

- [ ] **Step 3: Implement `coalesce_assignment`.** It looks up the newest
  `Notification` with `recipient_id=technician_id`,
  `kind=NotificationKind.VISIT_ASSIGNED`, `read_at__isnull=True`,
  `data__period=period` and `created_at__gte=(now or timezone.now()) -
  ASSIGNMENT_COALESCE_WINDOW`, using `select_for_update()` inside the caller's
  transaction. If it finds one, it increments `data["count"]`, saves
  `update_fields=["data", "updated_at"]` and returns `True`. It sends no
  e-mail. In `edit_planned_visit`, call it where the code now calls
  `announce_assignments`, and announce only when it returns `False`. Leave
  `bulk_assign` and the generator untouched.

- [ ] **Step 4: Run the tests to confirm they pass**

  Run: `uv run pytest tests/test_planned_visits.py tests/test_notifications.py -q`
  Expected: PASS. The existing
  `test_operations_reassigns_and_the_row_is_marked_edited` still sees
  `{"period": OCT, "count": 1}`.

- [ ] **Step 5: Commit**

  `git commit -am "SL-166-feat: assignments made one at a time fold into one notification"`

### Task 4: Contract, checks, PR

**Files:**
- Modify: `openapi/v1.yaml` (generated).
- Modify: `docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md`.
  Add a line under "API" pointing to the calendar spec for the PATCH rules,
  the new filters and coalescing.

- [ ] **Step 1: Regenerate the contract.** Run `make spec`. Expected: no
  warnings. The diff shows the three filters and the three error codes.
- [ ] **Step 2: Run the full gate.**
  `make lint && make mypy && make check && make test`. Expected: all pass.
- [ ] **Step 3: Commit.**
  `git commit -am "SL-166-chore: publish the calendar's API contract"`
- [ ] **Step 4: Open the PR.** Push `SL-166`, open a PR that closes its
  issue, and wait for CI to pass. Note in the PR that the web must translate
  the three codes (`lint:errors`) in web part A.
