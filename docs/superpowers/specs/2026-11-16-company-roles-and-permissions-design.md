# Company roles and permissions

Date: 2026-11-16. Status: design, awaiting the user's review. Covers
`shiftlush-api` and `shiftlush-web`.

## Why

Every firm gets the same five roles (owner, admin, operations, technician,
accountant) and the same fixed table of what each may do (`MATRIX` in
`core/permissions.py`). Nobody can change either from the product. Firms are
not shaped alike: one has a field supervisor who should see every fault but not
the money, another has an outside bookkeeper who should read the ledger and
nothing else. Today the only answer is "pick the least-wrong role".

The user's ask, in their words: firms define their own roles if they want to,
and assign permissions to them at the level of read, write and delete; the
company's administrator manages all of it.

## Decisions the user made

1. **Firms can define their own roles.** The five current roles stay, as ready
   roles every firm starts with.
2. **Permissions are granted per module as read / write / delete.**
3. **Scope is a property of the role, not of each module** (option B): a role
   either sees everything its grants allow, or only the work assigned to its
   user (today's technician behaviour). The firm's administrator can switch
   this on and off per role.
4. **Roles and grants live in the database** (approach 1, below).

Everything else in this document is a proposal made while writing it and is
marked *(call)*; the user reviews those.

## Goals

- A firm can create, rename, edit and delete its own roles.
- Each role has, per resource, a read / write / delete grant, and an
  "assigned work only" switch.
- The administrator assigns a role to each user and to each invitation.
- On the day this ships, **nobody's access changes**: every existing user ends
  up on a role whose answers are exactly today's `MATRIX`.
- The web shows and hides things from the permissions the server sends, not
  from role names.
- Every change to a role is in the audit log.

## Non-goals

- **Per-user exceptions.** A user has exactly one role. Someone who needs
  something different gets a role of their own *(call — reason: "why can Ayşe
  see this?" must have a single answer)*.
- Per-module scope ("all faults, but only my own visits").
- Field-level permissions beyond the slices that already exist
  (`contract_financials`, `ledger_admin`, …).
- Customer portal accounts. They have their own, separate permission model
  (`IsPortalAccount`) and are untouched.
- Permissions across firms. The tenant boundary stays where it is.

## Anti-goals (failure even if it works)

- A firm locks itself out (nobody left who can manage roles).
- Someone grants themselves, or anyone, more than they have.
- An existing user silently gains or loses access in the migration.
- The web and the server disagree about what a user may do.

## Approaches considered

1. **Roles and grants as rows in the database** — chosen. Each firm owns its
   role rows; users and invitations point to them by foreign key. Integrity
   (a role in use can't be deleted), audit and queries ("who holds this
   permission") come from the database. Costs the most: a data migration and
   moving every permission check to read rows.
2. **Keep the five-role enum and store a per-firm override as JSON.** Fast to
   start, but custom roles would be names inside a JSON blob: nothing stops a
   user pointing at a deleted role, and audit is weak. Cheap now, expensive
   later.
3. **Django's `Group`/`Permission`.** Rejected: groups are global rather than
   per firm, and model-level permissions can't express slices like
   `contract_financials`.

## Data model (API)

### `CompanyRole` (new, in `apps/users`)

| Field | Notes |
|---|---|
| `company` | FK, tenant-scoped like every company-owned model. |
| `name` | Unique per company among non-deleted roles. Max 60. |
| `description` | Optional. |
| `system_key` | `owner` / `admin` / `operations` / `technician` / `accountant` for the five seeded roles, empty for custom ones. Lets the web show the translated default name until the firm renames it *(call)*. |
| `is_owner` | True on exactly one role per company. See "The owner role". |
| `assigned_only` | Decision 3. Seeded true on `technician` only. |
| `field_staff` | *(call)* Whether users with this role can be assigned field work (visits, faults, planned visits) and land on the mobile start screen. Seeded true on `technician` only. Separate from `assigned_only` because a field supervisor sees everything but is still sent to lifts. |
| `is_deleted`, timestamps | Soft delete, as elsewhere. |

### `RoleGrant` (new)

`role` (FK), `resource` (string, one of the catalogue), `can_read`,
`can_write`, `can_delete`. Unique `(role, resource)`. A missing row means no
access. *(call: rows rather than a JSON column, so "who can delete invoices"
is one query and the constraint is the database's.)*

### Changes to existing models

- `User.role` (enum string) → `User.company_role` (FK to `CompanyRole`,
  `PROTECT`). Nullable only for users without a company.
- `Invitation.role` → `Invitation.company_role` (FK, `PROTECT`).
- The `Role` enum stays in code as the list of seed keys, nothing more.

### The resource catalogue (code, not data)

The list of resources stays in code: a resource only exists where a view
checks it. `MATRIX` becomes `CATALOGUE`, one entry per resource:

- which actions apply (`audit_log` can be read but not written; `ledger` has no
  write; `user_deactivation` has only write);
- the group it belongs to in the editor (Customers & sites, Lifts & work,
  Faults, Money, Company & team);
- the seed grants for each of the five system keys — today's `MATRIX`, with
  delete equal to write (see "Delete").

A grant for an action a resource does not have is refused on write
(`ACTION_NOT_AVAILABLE`). Adding a resource later means adding it to the
catalogue with its seed; a migration adds the rows for existing roles from the
seed, so nobody gains it by accident except where the seed says so.

New resource: `role` (read/write/delete roles and their grants). Seeded on
owner and admin.

## How a permission is checked

- `may(user, resource, action)` replaces `may(role, …)`. It reads the user's
  role grants, **loaded once per request** (one query, memoised on the
  request) *(call)*. A role change therefore takes effect on the user's next
  request, not when their token expires.
- The `role` claim is **removed from the access token** *(call)*. It would go
  stale the moment a role is edited, and nothing should trust it.
- `RolePermission` maps the HTTP method to an action: safe methods → read,
  `DELETE` → delete, everything else → write. A viewset's custom action that
  removes something (soft delete, cancel) declares `delete` through
  `resource_by_action`.
- `TechnicianScopedQueryset` and every `user.role == Role.TECHNICIAN` check
  become `user.company_role.assigned_only` (scope) or `.field_staff`
  (assignable). See the inventory below.

### Delete

Today delete is part of write. To keep behaviour identical, every seeded role
gets `can_delete = can_write`. From then on the firm can split them. Only
resources that actually have a delete path offer the column.

### The owner role

- Exactly one per company, created at registration, `is_owner = true`.
- Holds **every** action of every resource, computed rather than stored, so a
  resource added later is the owner's automatically.
- Can't be edited, renamed *(call: renaming allowed, grants not)*, deleted, or
  have `assigned_only` set.
- Only an owner can give the owner role to someone (today's rule in
  `team_views.py`). The "last active owner" guards in `users/services.py` stay.
- Company settings, ending a user's access and the books (`ledger_admin`) keep
  their current seeds, so out of the box only the owner (and the accountant
  for the books) holds them; a firm can now grant them to other roles.

## Guarding against escalation and lock-out

- **Editing is strict: you can only change what you hold** (user's decision,
  option A). Creating or editing a role may only add or remove actions the
  editor's own role holds; the check runs on the **changed** grants, not on the
  resulting set, so an admin can still edit a role that also carries grants the
  owner added (those are left as they are). An owner holds everything.
- **Assigning is as permissive as today** (user's decision, option A). Whoever
  holds `user` write (or `invitation` write) may give any role except the
  owner role to a user or an invitation, and may change the role of anyone who
  is not the last active owner — exactly today's rules (`team_views.py:98`,
  `team_serializers.py:118`, `users/services.py:673`). The accepted cost: an
  admin can hand someone access the admin lacks (e.g. the accountant's
  `ledger_admin`), as is already possible today. Nobody can add a permission
  to their own reach.
- **You can't edit your own role.** An admin changing the admin role would be
  editing their own reach *(call)*. The owner role can't be edited at all.
- The owner always holds `role` write, so a firm can't lock itself out.
- Deleting a role that users or pending invitations hold is refused
  (`ROLE_IN_USE`, with the count); reassign first.
- *(call)* At most 30 roles per company, to keep the picker usable.

## Scoped roles, field staff and assignments

Settled after the builder check *(calls)*:

- **A scoped role can't hold resources that are not narrowed.** Narrowing exists
  only where a viewset has a `customer_path` (customers, buildings, lifts,
  visits, planned visits, faults, inspections, attachments). Contracts,
  invoices, the ledger, customer reports, audit log, users, roles and company
  are not narrowed, so with `assigned_only` on, the editor offers no grant on
  them and the API refuses one (`NOT_AVAILABLE_FOR_SCOPED_ROLE`). Switching
  `assigned_only` on for a role that holds such grants is refused with the
  list. The catalogue marks each resource `scopable: true|false`. Widening the
  narrowing to money is a later, separate piece of work.
- **Customer assignments go to `field_staff` roles.** `assigned_only` requires
  `field_staff` (a scoped role that can't be assigned would see nothing).
  Today's `ONLY_TECHNICIANS_ARE_ASSIGNED` becomes "only field staff". A user
  moved to a role without `field_staff`, or whose role loses the flag, loses
  their assignments, as a technician changed to another role does today
  (`users/services.py:679`); turning the flag off asks for confirmation with
  the number of assignments that will go.
- **The technician's own-work upload right** (`attachments/api/v1/permissions.py`:
  photos and signatures on their own visit or fault without `attachment`
  write) belongs to `field_staff` users on work assigned to them. The parity
  test covers it alongside `MATRIX`.
- **Notifications** that today go to a fixed set of roles get their own
  catalogue entries with a single "receives" action, seeded so the recipients
  stay exactly the same: `notify_contract` (owner, admin — not operations,
  unlike `contract` write), `notify_fault_office`, `notify_inspection_office`,
  `notify_report`, `notify_billing`, `notify_plan`. A firm can then decide who
  receives what, which is part of what the user asked for.
- **The web learns of a role change** from `permissions` in the login and
  refresh responses (where it reads the user today) and from `GET
  /auth/me`, refetched when the tab regains focus and after any 403. A stale
  screen can at worst show a control the server then refuses.

## Behaviour that depends on a role name today

All of it has to move to a permission or to a role flag. Inventory from
`grep` on 2026-11-16:

| Where | Today | Becomes |
|---|---|---|
| `core/permissions.py` `MATRIX`, `may`, `RolePermission` | enum lookup | grants (above) |
| `may_see_balances` (customers views + serializer) | ledger read or document write | same rule over grants |
| `core/viewsets.py:84`, `inspections/api/v1/views.py:75`, `maintenance/api/v1/views.py:360`, `attachments` permissions/views | `role == TECHNICIAN` narrows queryset | `assigned_only` |
| `faults/api/v1/views.py:148`, `faults/api/v1/serializers.py:156,168`, `maintenance/api/v1/serializers.py:212` | a technician acts only on their own | `assigned_only` |
| `faults/services.py:75`, `inspections/services.py:152`, `maintenance/services.py:410`, `maintenance/api/v1/views.py:198`, `maintenance/tasks.py:62`, `users/services.py:679,693`, `team_serializers.py:49` | "the technicians" as a list of people to assign / notify | `field_staff` |
| `faults/services.py:42`, `inspections/services.py:42` `_OFFICE` | who is notified | users holding `fault_office` / `inspection` write |
| `reports/services.py:38` | owner + admin notified | users holding `customer_report_regenerate` write |
| `billing/services.py:45` | owner, admin, accountant | users holding `invoice` write |
| `contracts/tasks.py:37`, `maintenance/tasks.py:57` | owner + admin / office notified | users holding `contract` write / `planned_visit` write |
| `users/services.py:136,639-673`, `team_views.py:98`, `team_serializers.py:118` | owner rules | `is_owner` |
| `users/services.py:92` | `role` in the JWT | removed |
| `core/demo*.py`, `create_demo_account` | seed users by enum | seed by `system_key` |

Notification recipients by permission is a behaviour change only for custom
roles; with the seeded roles the recipients are the same people as today. A
test asserts that.

## API surface

- `GET /api/v1/me/` (or wherever the session user is served today) adds
  `role: {id, name, system_key, is_owner, assigned_only, field_staff}` and
  `permissions: {resource: ["read", "write", "delete"]}` — the effective set,
  owner included. The web uses nothing else.
- `GET /api/v1/company/roles/catalogue/` — resources, their groups and which
  actions apply. Labels are the web's (Turkish lives in `messages/tr.json`).
- `GET/POST /api/v1/company/roles/`, `GET/PATCH/DELETE /api/v1/company/roles/{id}/`
  — role with its grants inline; PATCH replaces the grant set. List includes
  `user_count` and `invitation_count`.
- `POST /api/v1/company/roles/{id}/duplicate/` — copy as a starting point.
- User and invitation endpoints take `company_role` (id) instead of `role`.
  *(call)* The old `role` string is accepted for one release as an alias of
  the seeded role with that key, and listed in `CHANGELOG-API.md` as
  deprecated, the way earlier contract changes were.
- New error codes: `ROLE_IN_USE`, `ROLE_LIMIT_REACHED`,
  `PERMISSION_NOT_HELD` (changing a grant you do not hold),
  `OWNER_ROLE_LOCKED`, `ACTION_NOT_AVAILABLE`, `DUPLICATE_ROLE_NAME`,
  `NOT_AVAILABLE_FOR_SCOPED_ROLE`.

## Audit

Role create, update (with a before/after of grants and flags), delete and
duplicate are audit events, as are role changes on users (already logged
today as a user update). `AuditLogListScreen` shows them with the grant diff.

## Web

- **One source of truth:** `useCan(resource, action)` and
  `useRole()` from the session, fed by `permissions` and `role` from the API.
- **Replace role checks.** About 30 files compare role names today; the largest
  is `components/layout/nav-config.ts` (62 hits), then `lib/visit.ts`,
  `lib/reports.ts`, `screens/list-searches.ts`, `UserDetailScreen`,
  `ElevatorDetailScreen`, `lib/plan.ts`, `lib/faults.ts`. Navigation items
  declare a resource instead of a role list. The technician's mobile start
  screen and "My work" labels key off `field_staff` / `assigned_only`.
  A lint script (like the existing `lint:*` checks) forbids comparing role
  names in `src/` outside the styleguide.
- **Settings → Roles** (new, for holders of `role` read):
  - list: name, user count, "assigned work only" badge, owner role pinned on top
    and locked;
  - editor: name, description, the two switches, and a grid — rows are
    resources grouped as in the catalogue, columns read / write / delete;
    a cell that does not apply is absent, a cell above the editor's own reach
    is disabled with a tooltip; ticking write ticks read, unticking read
    unticks the rest *(call)*;
  - "duplicate", and delete with the in-use count when refused.
- **Users and invitations:** the role picker lists the firm's roles, disabling
  those above the current user.
- The styleguide's role switcher becomes a permission-set switcher.

## Migration and rollout

Each step is one PR per repo (or API only), in order, each shippable alone:

1. **API — model and parity.** Add `CompanyRole`/`RoleGrant`, seed the five
   roles per existing company from `MATRIX`, point every user and pending
   invitation at the seeded role with the same key. Switch `may` and the
   inventory above to grants and flags. A **parity test** walks every resource
   × action × seeded role and asserts the new answer equals the old `MATRIX`;
   the full suite must pass unchanged. JWT `role` claim removed.
2. **API — `/me` permissions and role endpoints**, guards, audit, error codes,
   `role` alias on user/invitation endpoints.
3. **Web — permission-driven UI.** `useCan`, nav and screens off role names,
   the lint check. No visible change for anyone.
4. **Web — Roles screen** and the new role pickers.
5. **API — drop the `role` alias** one release later.

Rollback: steps 1–2 keep the old enum values derivable (`system_key`), so a
revert of step 1 can be a reverse migration that writes `role` back from
`company_role.system_key`, refusing if any user holds a custom role.

## Acceptance criteria

- [ ] After step 1, the parity test passes for all resources, actions and the
      five seeded roles, and the existing suites pass without edits other than
      fixtures.
- [ ] A firm can create a role, grant read/write/delete per resource, toggle
      "assigned work only", and assign it to a user and to an invitation.
- [ ] A user whose role lacks `delete` on a resource gets 403 on `DELETE` and
      sees no delete control on the web.
- [ ] A user with `assigned_only` sees only assigned customers, visits, faults
      and inspections, exactly as a technician does today.
- [ ] Changing a grant one does not hold is refused with
      `PERMISSION_NOT_HELD`, while an admin can still invite an accountant; editing the owner role with `OWNER_ROLE_LOCKED`;
      deleting a role in use with `ROLE_IN_USE`.
- [ ] A role change takes effect on the affected user's next request.
- [ ] Every role change appears in the audit log with the grant diff.
- [ ] No file in `shiftlush-web/src` (outside the styleguide) compares role
      names; the lint check enforces it.

## Left to the builder

- Exact grouping and ordering of resources in the editor.
- Query shape for notification recipients by permission.
- Whether the per-request permission cache is a request attribute or a
  `cached_property` on the user object.
- Copy for the editor, in Turkish, in `messages/tr.json`.
