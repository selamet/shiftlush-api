# Maintenance visit record — design

Status: approved, not yet implemented.
Scope: `shiftlush-api` only. The frontend screen is a separate, later piece of work.

## Context

The Phase 1 specification (`faz1-sartname.md`, line 5 and line 137) explicitly
puts five things out of scope for Phase 1, leaving only data-model
placeholders behind so Phase 2 is not blocked:

1. Maintenance plan generation
2. **Maintenance visit records (this document)**
3. Fault management
4. Periodic (statutory) inspection module
5. Notification/reminder engine

These five are independent subsystems with real dependencies between them —
plan generation and the reminder engine both need to know what maintenance
has actually happened, which only visit records can answer. This document
scopes down to the second item: the record a technician fills in for a
routine monthly maintenance visit.

**This is not the periodic inspection module.** `Elevator.last_inspection_date`,
`next_inspection_date`, `inspection_body`, `inspection_report_number` and
`inspection_label` describe an independent third-party statutory inspection
(`periyodik kontrol`) — a different legal event from the company's own
technician doing the monthly maintenance visit this document covers. The two
must not be conflated in the data model or the UI.

## Data model

New Django app: `apps/maintenance`.

### `MaintenanceVisit` (`CompanyOwnedModel`)

| Field | Type | Notes |
|---|---|---|
| `elevator` | FK → `elevators.Elevator`, `PROTECT` | |
| `contract` | FK → `contracts.Contract`, `PROTECT` | Must be an active contract for the elevator at `visit_date`; enforced in the service layer, not just a DB constraint (see Business logic) |
| `technician` | FK → `users.User`, `PROTECT` | Who performed the visit |
| `visit_date` | `DateField` | Can be backdated (entered after the fact) |
| `status` | `CharField`, choices | `COMPLETED`, `INCOMPLETE`, `ACCESS_DENIED` |
| `status_note` | `TextField`, blank | Why, when not `COMPLETED` |
| `work_notes` | `TextField`, blank | Free-text summary |
| `parts_replaced` | `TextField`, blank | Free text; a dedicated parts/inventory model is out of scope (YAGNI) — nothing today reports on parts usage |

`Meta.ordering = ["-visit_date", "id"]` from the start — see the SL-118 lesson
below.

### `MaintenanceVisitItem`

One row per checklist item per visit.

| Field | Type | Notes |
|---|---|---|
| `visit` | FK → `MaintenanceVisit`, `CASCADE` | A real child row, not a shared reference |
| `item` | `CharField`, choices (`ChecklistItem`) | See list below |
| `result` | `CharField`, choices | `OK`, `ATTENTION_NEEDED`, `NOT_APPLICABLE` |
| `note` | `TextField`, blank | |

`UniqueConstraint(fields=["visit", "item"])` — each checklist item appears at
most once per visit.

**Why a related row model and not a JSON field.** The only `JSONField` usage
in the codebase today is `audit.old_values`/`new_values`, a generic diff
payload with no fixed shape. Every piece of actual domain data — including
per-line data like `ContractElevator` — is modelled relationally with
choices and constraints. A JSON blob here would be the first exception to
that pattern and would block the one thing a checklist is for later:
reporting which item fails most often across the fleet.

**Checklist items (`ChecklistItem` `TextChoices`, English value + label per
existing convention — see `apps/users/models.py:18-19`, Turkish labels live
in the frontend, never in this enum):**

```
BRAKE_SYSTEM              Brake system
HOISTING_ROPES            Hoisting ropes and cables
LANDING_DOORS             Landing doors and locks
CAR_DOOR_SAFETY_EDGE      Car door and safety edge
LIMIT_SWITCHES            Limit switches
GUIDE_RAIL_LUBRICATION    Guide rail lubrication
CAR_LIGHTING              Car lighting
EMERGENCY_LIGHTING        Emergency lighting
EMERGENCY_ALARM_INTERCOM  Emergency alarm / intercom
CONTROL_PANEL_BUTTONS     Control panel and call buttons
LEVELING_ACCURACY         Floor leveling accuracy
OVERLOAD_DEVICE           Overload sensor
AUTOMATIC_RESCUE_DEVICE   Automatic rescue device (ARD) test
PIT_CONDITION             Pit condition and cleanliness
MACHINE_ROOM_CONDITION    Machine room condition and ventilation
EARTHING_GROUNDING        Earthing / grounding
```

This is a default, generic list (the user has no company-specific checklist
standard to match). It can grow by adding an enum member — no migration
touches `MaintenanceVisit` itself when it does.

### `Elevator` additions

Two new fields, mirroring the existing `last_inspection_date` /
`next_inspection_date` pattern:

- `last_maintenance_date` (`DateField`, null, blank)
- `next_maintenance_date` (`DateField`, null, blank, indexed — same reasoning
  as the existing `Index(fields=["next_inspection_date"])`)

### `Attachment`

`ObjectType` gains `MAINTENANCE_VISIT`. Photos are optional, category
`PHOTO`, `object_id` = the visit's id.

## Business logic

`apps/maintenance/services.py::recalculate_next_maintenance(elevator)`:

- Finds the most recent `MaintenanceVisit` with `status=COMPLETED` for the
  elevator (by `visit_date`, not by creation order — a visit can be entered
  out of order).
- Sets `elevator.last_maintenance_date` to that visit's `visit_date`.
- Sets `elevator.next_maintenance_date` to `last_maintenance_date +
  maintenance_interval_days`.
- If no `COMPLETED` visit exists, both fields go back to `None`.

Called after every create, update, and delete of a `MaintenanceVisit` for
that elevator — recomputed from source each time, never incremented in
place. This is what keeps a backdated `COMPLETED` entry, or an edit that
changes a visit's status away from `COMPLETED`, from leaving a stale or
wrong `next_maintenance_date` behind.

**Active contract requirement.** Creating or updating a `MaintenanceVisit`
requires the named `contract` to be `ACTIVE` (per `Contract.status`) and to
cover `elevator` (via `ContractElevator`) as of `visit_date`. Checked in the
service layer alongside the recalculation above, not left to a DB
constraint — the same place that already reasons about contract state
elsewhere in the codebase (see `apps/contracts/services.py`).

## API and permissions

Two additions to `core/permissions.MATRIX`:

```python
"maintenance_visit": {
    READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
},
"maintenance_visit_edit": {
    READ: set(),
    WRITE: {Role.OWNER, Role.ADMIN},
},
```

`MaintenanceVisitViewSet(TenantViewSet[MaintenanceVisit])`:

- `resource = "maintenance_visit"`
- `resource_by_action = {"update": "maintenance_visit_edit", "partial_update":
  "maintenance_visit_edit", "destroy": "maintenance_visit_edit"}` — the same
  mechanism `resource_by_action` already exists for (see the QR-label
  docstring in `core/permissions.py`): a technician may create a visit but
  not edit or delete one afterwards; only `OWNER`/`ADMIN` can.
- `customer_path = "elevator__customer_id"` — gives the existing
  `TechnicianScopedQueryset` narrowing for free.
- `ordering_fields = ["visit_date", "created_at"]`, `ordering = ["-visit_date",
  "id"]` — explicit from day one. SL-118 (#79) found that an aggregate
  `.annotate(Count(...))` on a list queryset silently drops `Meta.ordering`
  because of the `GROUP BY` it introduces; setting the viewset's own
  `ordering` attribute is what actually survives that. This viewset has no
  such annotation planned, but the explicit attribute costs nothing and
  removes the question later.
- `items` (the checklist rows) are set via a separate action —
  `POST /maintenance-visits/{id}/set-checklist/`, replacing the full set in
  one call — the same shape `Contract`/`ContractElevator` already uses via
  `add-elevators`, rather than a writable nested serializer on
  `MaintenanceVisit` itself. See the implementation plan
  (`docs/superpowers/plans/2026-09-12-maintenance-visit.md`) for why this
  changed during planning.

**Attachment permission carve-out.** Today `MATRIX["attachment"][WRITE]` is
`{OWNER, ADMIN, OPERATIONS}` — a `TECHNICIAN` cannot upload any attachment.
Widening that matrix entry to include `TECHNICIAN` would let a technician
upload signed contracts and CE certificates too, which is not the intent.
Instead, `RolePermission.has_permission` (or `AttachmentViewSet` directly)
gets a narrow, explicit exception: a `TECHNICIAN` may `POST` to the
attachment endpoint only when the payload's `object_type ==
ObjectType.MAINTENANCE_VISIT` and `category == AttachmentCategory.PHOTO`;
every other combination still falls through to the matrix and is denied. The
existing tenant/technician-assignment narrowing on the referenced visit
still applies — a technician cannot attach a photo to a visit outside their
assigned customers.

Endpoints: `GET/POST /api/v1/maintenance-visits/`, `GET/PATCH/DELETE
/api/v1/maintenance-visits/{id}/`. Filters: `elevator`, `customer` (via
`elevator__customer_id`), `status`, `technician`, `visit_date` range.

## Testing plan

Following TDD, one test per decision point:

- Creating a visit without an active contract for the elevator is rejected
- A `TECHNICIAN` cannot create a visit for a customer outside their
  assignments (403 via the existing narrowing)
- A `TECHNICIAN` can create a visit for an assigned customer
- A `TECHNICIAN` cannot `PATCH` or `DELETE` a visit they created; `OWNER`/
  `ADMIN` can
- `status=INCOMPLETE` or `ACCESS_DENIED` does not move
  `next_maintenance_date`
- A backdated `COMPLETED` visit (earlier `visit_date` than the current
  latest) does not move `last_maintenance_date`/`next_maintenance_date`
  backwards
- Deleting the most recent `COMPLETED` visit recalculates
  `next_maintenance_date` from whatever is now the latest one (or clears it
  if none remain)
- A `TECHNICIAN` can upload a `photo` attachment against their own
  `maintenance_visit`; cannot upload a `signed_contract` or attach to a
  `contract`
- The list endpoint does not raise `UnorderedObjectListWarning`
  (`tests/test_pagination_ordering.py` pattern)
- `make lint && make test && make check && make mypy` pass

## Out of scope (explicitly)

- Maintenance plan generation, fault management, the periodic inspection
  module, and the notification/reminder engine — separate subsystems, each
  gets its own design once this one ships.
- A parts/inventory model — `parts_replaced` stays free text until something
  actually needs to query it structurally.
- Digital signatures / customer sign-off.
- The frontend (`shiftlush-web`) screen.
