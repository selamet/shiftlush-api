# Company Roles and Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Firms define their own roles with per-resource read / write / delete grants and two role flags (`assigned_only`, `field_staff`), managed in Settings, without changing anyone's access on the day it ships.

**Architecture:** Roles and grants become per-company rows (`CompanyRole`, `RoleGrant`) seeded from a code-defined resource catalogue that replaces `MATRIX`. Every permission check, scope check and notification recipient query reads the user's role rows (loaded once per request). The web stops comparing role names and reads `role` + `permissions` from the session user.

**Tech Stack:** Django 6.1 + DRF + drf-spectacular, pytest (API, `uv run`); React + TanStack Router/Query + i18next + vitest (web).

**Spec:** `docs/superpowers/specs/2026-11-16-company-roles-and-permissions-design.md` (approved 2026-11-16). Read it before any task; this plan does not repeat its reasoning.

## Global Constraints

- Delivered as **five PRs, in order**: Part A (API, PR1), Part B (API, PR2), Part C (web, PR3), Part D (web, PR4), Part E (API, PR5). Each part ships alone; each is its own `SL-<n>` branch, issue and PR (see the repo's git workflow; the number is the next free one across both repos).
- Commit messages: `SL-<n>-<type>: <english description>`, no AI attribution lines.
- After Part A, **no existing user's answer to any permission question changes** — enforced by the parity tests in A3 and A5.
- Seed keys: `owner`, `admin`, `operations`, `technician`, `accountant`. `assigned_only` and `field_staff` seeded true on `technician` only.
- Seeded `can_delete = can_write` on every resource that has a delete action.
- At most **30** roles per company (non-deleted). Role `name` max length **60**, unique per company among non-deleted roles.
- New error codes, exactly: `ROLE_IN_USE`, `ROLE_LIMIT_REACHED`, `PERMISSION_NOT_HELD`, `OWNER_ROLE_LOCKED`, `ACTION_NOT_AVAILABLE`, `DUPLICATE_ROLE_NAME`, `NOT_AVAILABLE_FOR_SCOPED_ROLE`, `OWN_ROLE_LOCKED`, `ASSIGNED_ONLY_NEEDS_FIELD_STAFF`.
- Turkish copy only in `shiftlush-web/messages/tr.json`; code, comments and API docs in English.
- API contract changes go into `openapi/v1.yaml` (`make` target at `Makefile:41`) and `CHANGELOG-API.md`; the web syncs with `npm run api:sync` + `npm run api:generate`.

## Review Focus

1. **A company created after the migration** (registration, `create_demo_account`, tests that build a `Company` directly) has its five seeded roles — otherwise its owner can do nothing. Test in A2.
2. **A user with no company or no role** (superuser, portal account) — `may()` returns `False` and nothing raises. Test in A3.
3. **An access token minted before Part A** still carries a `role` claim — it must be ignored, not trusted and not rejected. Test in A6.
4. **Demoting or reassigning the last active owner** through the new `company_role` field is refused exactly as today. Test in B3.
5. **Turning `field_staff` off on a role whose users hold customer assignments** removes them only after confirmation (`confirm=true`); without it the API answers 409 with the count. Test in B2.

---

# Part A — API: roles as data, identical behaviour (PR1)

### Task A1: The resource catalogue

**Files:**
- Create: `core/permission_catalogue.py`
- Create: `tests/legacy_matrix.py` (a frozen copy of today's `MATRIX` dict from `core/permissions.py:39-211`, verbatim, used only by tests)
- Test: `tests/test_permission_catalogue.py`

**Interfaces:**
- Produces:
  - `READ = "read"; WRITE = "write"; DELETE = "delete"; RECEIVE = "receive"` (move `READ`/`WRITE` here; `core/permissions.py` re-exports them)
  - `@dataclass(frozen=True) class Resource: key: str; actions: frozenset[str]; group: str; scopable: bool; seeds: Mapping[str, frozenset[str]]` (seed key → actions held)
  - `CATALOGUE: dict[str, Resource]`
  - `SEED_KEYS: tuple[str, ...] = ("owner", "admin", "operations", "technician", "accountant")`
  - `GROUPS = ("customers", "work", "faults", "money", "company")`

- [ ] **Step 1: Write the failing tests**

```python
def test_every_legacy_resource_is_in_the_catalogue():
    assert set(LEGACY_MATRIX) <= set(CATALOGUE)

@pytest.mark.parametrize("resource", sorted(LEGACY_MATRIX))
@pytest.mark.parametrize("key", SEED_KEYS)
def test_seeds_reproduce_the_legacy_matrix(resource, key):
    legacy = LEGACY_MATRIX[resource]
    seeds = CATALOGUE[resource].seeds.get(key, frozenset())
    assert (READ in seeds) == (key in legacy[READ])
    assert (WRITE in seeds) == (key in legacy[WRITE])
    if DELETE in CATALOGUE[resource].actions:
        assert (DELETE in seeds) == (key in legacy[WRITE])

def test_seeds_only_use_actions_the_resource_has():
    for r in CATALOGUE.values():
        for held in r.seeds.values():
            assert held <= r.actions

def test_role_resource_is_seeded_on_owner_and_admin():
    assert CATALOGUE["role"].seeds["admin"] == {READ, WRITE, DELETE}
    assert "operations" not in CATALOGUE["role"].seeds

def test_notification_resources_match_todays_recipients():
    expected = {
        "notify_contract": {"owner", "admin"},                      # contracts/tasks.py:37
        "notify_fault_office": {"owner", "admin", "operations"},    # faults/services.py:42
        "notify_inspection_office": {"owner", "admin", "operations"},  # inspections/services.py:42
        "notify_report": {"owner", "admin"},                        # reports/services.py:38
        "notify_billing": {"owner", "admin", "accountant"},         # billing/services.py:45
        "notify_plan": {"owner", "admin", "operations"},            # maintenance/tasks.py:57
    }
    for key, holders in expected.items():
        assert CATALOGUE[key].actions == {RECEIVE}
        assert {k for k, a in CATALOGUE[key].seeds.items() if RECEIVE in a} == holders

def test_scopable_is_exactly_the_narrowed_resources():
    assert {k for k, r in CATALOGUE.items() if r.scopable} == {
        "customer", "building", "elevator", "maintenance_visit", "planned_visit",
        "fault", "inspection", "attachment", "qr_label",
    }
```

- [ ] **Step 2: Run, expect ImportError** — `uv run pytest tests/test_permission_catalogue.py -q`
- [ ] **Step 3: Write `core/permission_catalogue.py`.** One `Resource` per `MATRIX` entry plus `role` and the six `notify_*`. `actions` per resource: read-only ones (`audit_log`, `ledger`, `ledger_picker`) have `{READ}`; write-only ones (`user_deactivation`, `fault_office`, `maintenance_visit_edit`, `customer_report_regenerate`, `ledger_statement_send`) have `{WRITE}`; resources whose viewset exposes `DELETE` or a soft-delete action (check each viewset's `http_method_names`/actions; at least `customer`, `building`, `complex`, `elevator`, `contract`, `attachment`, `user`, `invitation`, `role`, `planned_visit`, `ledger_document`) also get `DELETE`. Keep the existing comments from `MATRIX` on each entry.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `SL-<n>-feat: the permission catalogue, seeded from today's matrix`

### Task A2: `CompanyRole`, `RoleGrant` and the migration

**Files:**
- Modify: `apps/users/models.py` (add models; `User.company_role`, `Invitation.company_role`; compat `role` property)
- Create: `apps/users/roles.py` (`seed_company_roles`)
- Create: `apps/users/migrations/0007_company_roles.py` (schema), `0008_seed_company_roles.py` (data), `0009_drop_user_role_column.py`
- Modify: `apps/companies/models.py` or `apps/users/signals.py` (seed on company creation)
- Test: `tests/test_company_roles_model.py`

**Interfaces:**
- Consumes: `CATALOGUE`, `SEED_KEYS` (A1).
- Produces:
  - `class CompanyRole(CompanyOwnedModel)`: `name: CharField(60)`, `description: TextField(blank=True)`, `system_key: CharField(20, blank=True)`, `is_owner: BooleanField(default=False)`, `assigned_only: BooleanField(default=False)`, `field_staff: BooleanField(default=False)`. Constraints: unique `(company, name)` where not deleted; unique `(company,)` where `is_owner`; unique `(company, system_key)` where `system_key != ""` and not deleted; check `assigned_only → field_staff`.
  - `class RoleGrant(models.Model)`: `role: FK(CompanyRole, CASCADE, related_name="grants")`, `resource: CharField(60)`, `can_read`, `can_write`, `can_delete`, `can_receive` (all `BooleanField(default=False)`). Unique `(role, resource)`.
  - `User.company_role: FK(CompanyRole, PROTECT, null=True, related_name="users")`; `Invitation.company_role: FK(CompanyRole, PROTECT, related_name="invitations")`.
  - `User.role` / `Invitation.role`: **compat property**, removed in Part E. Getter returns `company_role.system_key` (or `""` for a custom role / no role). Setter resolves the seeded role with that key in `self.company` and assigns `company_role`. Exists so the ~34 test call sites with `role=Role.X` and `create_user(role=...)` keep working.
  - `seed_company_roles(company: Company) -> dict[str, CompanyRole]` — idempotent; creates the five roles (names are the English labels of `Role`; the web translates by `system_key` until renamed) and their grants from `CATALOGUE` seeds. The owner role gets **no** grant rows (computed, see A3).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_new_company_gets_the_five_roles(db):
    company = make_company()
    keys = set(CompanyRole.unscoped.filter(company=company).values_list("system_key", flat=True))
    assert keys == set(SEED_KEYS)

def test_only_the_technician_is_scoped_and_field_staff(db): ...
def test_seeding_twice_creates_nothing_new(db): ...
def test_create_user_with_a_role_string_points_at_the_seeded_role(db):
    user = make_user(company, role=Role.ACCOUNTANT, email="a@x.test")
    assert user.company_role.system_key == "accountant" and user.role == "accountant"
def test_a_role_in_use_cannot_be_hard_deleted(db):  # PROTECT
def test_assigned_only_requires_field_staff(db):     # IntegrityError
```

- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement models, `seed_company_roles`, the `post_save` hook on `Company` (created only), and migrations.** `0007` adds tables and nullable FKs; `0008` (`RunPython`, reversible) seeds every existing company and sets `company_role` from the old `role` column for users and invitations; reverse writes `role` back from `system_key` and **raises** if any user holds a custom role. `0009` drops the old `role` columns and their check constraints (`user_role_valid`, `invitation_role_valid`).
- [ ] **Step 4: Run the new tests, then the whole suite** — `uv run pytest -q`. Expected: only failures are tests filtering querysets by `role=`; fix those call sites to `company_role__system_key=` in tests. Production filters are rewritten in A4/A5, so production code that still filters by `role` must fail here — list them and leave them for A4/A5 if the suite cannot be green otherwise, but the PR is not opened until A6 leaves it green.
- [ ] **Step 5: Commit** — `SL-<n>-feat: company roles and grants as rows, seeded for every firm`

### Task A3: Evaluating permissions from rows

**Files:**
- Modify: `core/permissions.py` (remove `MATRIX`; new `may`, `effective_permissions`, `may_see_balances`; `RolePermission` maps `DELETE`)
- Create: `tests/test_permission_parity.py`

**Interfaces:**
- Consumes: A1, A2.
- Produces:
  - `effective_permissions(user: User | None) -> dict[str, frozenset[str]]` — owner role: every action of every resource; otherwise from grants. Memoised on the user instance (`user._perm_cache`), so one query per request.
  - `may(user: User | None, resource: str, action: str) -> bool`
  - `may_see_balances(user: User | None) -> bool` (same rule as today, now on the user)
  - `RolePermission`: safe methods → `READ`, `DELETE` → `DELETE`, other → `WRITE`; `resource_by_action` may map an action to `(resource, action)` tuple to declare a soft-delete as `DELETE`.

- [ ] **Step 1: Write the failing parity test**

```python
@pytest.mark.parametrize("resource", sorted(LEGACY_MATRIX))
@pytest.mark.parametrize("key", SEED_KEYS)
@pytest.mark.parametrize("action", [READ, WRITE])
def test_seeded_roles_answer_like_the_legacy_matrix(db, resource, key, action):
    user = make_user(make_company(), role=key, email=f"{key}@x.test")
    assert may(user, resource, action) == (key in LEGACY_MATRIX[resource][action])

def test_no_user_and_no_role_may_nothing(db):
    assert may(None, "customer", READ) is False
    assert may(make_user_without_company(), "customer", READ) is False

def test_permissions_are_loaded_once_per_user(db, django_assert_num_queries): ...
def test_delete_follows_can_delete_not_can_write(db):  # custom role write w/o delete → 403 on DELETE /customers/{id}/
```

- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement; update every `may(role, …)` / `may_see_balances(role)` caller** (`apps/customers/api/v1/views.py:86`, `serializers.py:188`, anything `grep -rn "may(" apps core` finds) to pass the user.
- [ ] **Step 4: `uv run pytest -q` — PASS.**
- [ ] **Step 5: Commit** — `SL-<n>-feat: permissions read from the user's role, delete is its own action`

### Task A4: Scope and field staff instead of `Role.TECHNICIAN`

**Files (every line from the spec's inventory table):**
- Modify: `core/viewsets.py:84`, `apps/inspections/api/v1/views.py:75`, `apps/maintenance/api/v1/views.py:198,360`, `apps/attachments/api/v1/permissions.py:101-125`, `apps/attachments/api/v1/views.py:74`, `apps/faults/api/v1/views.py:148`, `apps/faults/api/v1/serializers.py:156,168`, `apps/maintenance/api/v1/serializers.py:212`, `apps/faults/services.py:75`, `apps/inspections/services.py:152`, `apps/maintenance/services.py:410`, `apps/maintenance/tasks.py:62`, `apps/users/services.py:679,693`, `apps/users/api/v1/team_serializers.py:49`
- Test: `tests/test_role_flags.py`

**Interfaces:**
- Produces on `User`: `is_scoped -> bool` (`company_role.assigned_only`), `is_field_staff -> bool` (`company_role.field_staff`), both `False` without a role. Querysets for "the field staff" use `company_role__field_staff=True`.

- [ ] **Step 1: Write the failing tests** — one per behaviour, each run for a *custom* role, since the seeded technician is already covered by the existing suites:
  - `test_a_custom_scoped_role_sees_only_assigned_customers` (and buildings, elevators, visits, faults, inspections)
  - `test_an_unscoped_field_staff_role_sees_every_fault_and_can_be_assigned_one`
  - `test_a_field_staff_user_uploads_to_their_own_visit_without_attachment_write`
  - `test_a_non_field_staff_role_cannot_receive_customer_assignments` → `ONLY_FIELD_STAFF_ARE_ASSIGNED` (rename of `ONLY_TECHNICIANS_ARE_ASSIGNED`; keep the old code as an alias value in `ErrorCode` only if the web reads it — `grep` the web's `messages/tr.json`)
  - `test_moving_a_user_to_a_non_field_staff_role_clears_their_assignments`
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Replace each `role == Role.TECHNICIAN` with `user.is_scoped` (visibility/“own work only” checks) or `user.is_field_staff` (who can be assigned / listed as technicians / own-work uploads)**, following the spec's table column "Becomes".
- [ ] **Step 4: `uv run pytest -q` — PASS.**
- [ ] **Step 5: Commit** — `SL-<n>-refactor: scope and field work follow role flags, not the technician role`

### Task A5: Notification recipients by permission

**Files:**
- Modify: `apps/faults/services.py:42`, `apps/inspections/services.py:42`, `apps/reports/services.py:38`, `apps/billing/services.py:45`, `apps/contracts/tasks.py:37`, `apps/maintenance/tasks.py:57`
- Modify: `core/permissions.py` (add helper)
- Test: `tests/test_notification_recipients.py`

**Interfaces:**
- Produces: `holders(company_id: UUID, resource: str, action: str) -> QuerySet[User]` — active, non-deleted users whose role is the owner role or has the grant.

- [ ] **Step 1: Write the failing test** — for each of the six call sites, build a company with one user per seeded role and assert the recipient set equals the legacy role set from A1's `test_notification_resources_match_todays_recipients`; plus `test_a_custom_role_with_receive_gets_contract_notices`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Replace each `role__in=[…]` / `_OFFICE` tuple with `holders(company_id, "notify_…", RECEIVE)`.**
- [ ] **Step 4: `uv run pytest -q` — PASS.**
- [ ] **Step 5: Commit** — `SL-<n>-refactor: notification recipients are whoever holds the notice`

### Task A6: Owner rules, token claim, demo data

**Files:**
- Modify: `apps/users/services.py:92,136,639-673`, `apps/users/api/v1/team_views.py:98`, `apps/users/api/v1/team_serializers.py:118`, `core/demo.py`, `core/demo_activity.py`, `core/management/commands/create_demo_account.py:148,167`, `core/authentication.py` (if it reads `role`)
- Test: `tests/test_owner_role.py`

- [ ] **Step 1: Failing tests:** `test_the_access_token_carries_no_role_claim`; `test_a_token_with_a_stale_role_claim_is_still_accepted_and_ignored` (mint one with `role="owner"` for an operations user; `GET /customers/` write is still 403); `test_register_company_makes_the_registrant_owner`; `test_last_active_owner_cannot_be_moved_off_the_owner_role`; `test_demo_account_seeds_users_on_seeded_roles`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Replace `Role.OWNER` comparisons with `company_role.is_owner`; delete the `access["role"]` line; demo seeding uses `seed_company_roles(...)[key]`.**
- [ ] **Step 4: Full verification** — `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy . && make schema` (Makefile:41; `openapi/v1.yaml` changes only where `role` is now derived). Expected: all green, schema diff limited to user/invitation `role`.
- [ ] **Step 5: Commit, open PR1.** CHANGELOG: "Roles are per company; nothing changes for existing users."

---

# Part B — API: role management (PR2)

### Task B1: Session user carries `role` and `permissions`

**Files:**
- Modify: `apps/users/api/v1/serializers.py:135-160` (`CurrentUserSerializer`, used by `/auth/me`, login and refresh via `_token_response`)
- Test: `tests/test_me_permissions.py`

**Interfaces:**
- Produces in the payload: `role: {id, name, system_key, is_owner, assigned_only, field_staff}` (object, replaces the string) and `permissions: dict[str, list[str]]` sorted, from `effective_permissions`. Keep a deprecated `role_key: str` (= `system_key`) for one release so the current web keeps working until Part C ships.

- [ ] **Step 1: Failing tests:** `test_me_lists_effective_permissions_for_an_accountant` (contains `"ledger_admin": ["read", "write"]`, no `"fault"`); `test_owner_gets_every_action_of_every_resource`; `test_login_and_refresh_return_the_same_shape_as_me`.
- [ ] **Step 2–4: Run red, implement, run green.**
- [ ] **Step 5: Commit** — `SL-<n>-feat: the session user carries its role and permissions`

### Task B2: Role endpoints and their guards

**Files:**
- Create: `apps/users/api/v1/role_views.py`, `apps/users/api/v1/role_serializers.py`, `apps/users/role_services.py`
- Modify: `apps/users/api/v1/urls.py`, `core/error_codes.py`
- Test: `tests/test_role_api.py`

**Interfaces:**
- Routes (names in parentheses): `GET company/roles/catalogue/` (`role-catalogue`), `GET|POST company/roles/` (`role-list`), `GET|PATCH|DELETE company/roles/{id}/` (`role-detail`), `POST company/roles/{id}/duplicate/` (`role-duplicate`). Resource `role`.
- Role payload: `{id, name, description, system_key, is_owner, assigned_only, field_staff, grants: {resource: [actions]}, user_count, invitation_count}`. PATCH replaces `grants` wholesale; `confirm: bool` (write-only) for flag changes that drop assignments.
- `role_services.update_role(role, editor, *, name, description, assigned_only, field_staff, grants, confirm) -> CompanyRole`, `create_role(...)`, `delete_role(role, editor)`, `duplicate_role(role, editor, name) -> CompanyRole` — all in one transaction.

- [ ] **Step 1: Failing tests (one each):**
  - `test_catalogue_lists_groups_actions_and_scopable`
  - `test_admin_creates_a_role_with_grants_they_hold`
  - `test_adding_a_grant_the_editor_lacks_is_refused` → 400 `PERMISSION_NOT_HELD` (admin adds `ledger_admin` read)
  - `test_admin_can_edit_a_role_carrying_grants_they_lack_if_they_leave_them_untouched` (owner put `ledger_admin` on a role; admin renames it and toggles `customer` write → 200, `ledger_admin` still there)
  - `test_removing_a_grant_the_editor_lacks_is_refused`
  - `test_the_owner_role_is_locked` → `OWNER_ROLE_LOCKED` on grants/flags/delete; rename allowed
  - `test_nobody_edits_their_own_role` → `OWN_ROLE_LOCKED`
  - `test_an_action_the_resource_lacks_is_refused` → `ACTION_NOT_AVAILABLE` (`audit_log: ["write"]`)
  - `test_a_scoped_role_cannot_hold_money` → `NOT_AVAILABLE_FOR_SCOPED_ROLE` (`assigned_only=true`, `invoice: ["read"]`), and switching `assigned_only` on for a role holding `invoice` read lists `invoice`
  - `test_assigned_only_without_field_staff_is_refused` → `ASSIGNED_ONLY_NEEDS_FIELD_STAFF`
  - `test_turning_field_staff_off_asks_first` → 409 with `{"assignments": N}`; with `confirm=true` → 200 and assignments gone
  - `test_a_role_in_use_cannot_be_deleted` → 409 `ROLE_IN_USE` with `users`, `invitations` counts
  - `test_the_31st_role_is_refused` → `ROLE_LIMIT_REACHED`
  - `test_names_are_unique_per_company` → `DUPLICATE_ROLE_NAME`; same name in another company is fine
  - `test_duplicate_copies_grants_and_flags_but_not_system_key_or_owner`
  - `test_operations_cannot_read_roles` → 403
  - `test_a_role_change_applies_on_the_next_request` (remove `customer` write from operations; same token, next POST → 403)
  - `test_role_changes_are_audited_with_grant_rows` (audit rows for `users.CompanyRole` and `users.RoleGrant`)
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.** The changed-grant check: compute `added = new − old` and `removed = old − new` per `(resource, action)`; every pair in `added ∪ removed` must be in `effective_permissions(editor)`. Register the new codes in `core/error_codes.py` with English labels.
- [ ] **Step 4: Run green; `make schema`.**
- [ ] **Step 5: Commit** — `SL-<n>-feat: firms manage their own roles`

### Task B3: Users and invitations take `company_role`

**Files:**
- Modify: `apps/users/api/v1/team_views.py:41,74,97-98,231`, `apps/users/api/v1/team_serializers.py:33,64,87,118`, `apps/users/services.py:673`
- Modify: `CHANGELOG-API.md`, `openapi/v1.yaml`
- Test: `tests/test_team_roles.py`

**Interfaces:**
- Write field `company_role: UUID` on user update and invitation create. Deprecated write alias `role: str` (seed key) resolved to the seeded role; if both are sent, `company_role` wins. Read payloads return `company_role` as `{id, name, system_key}`.

- [ ] **Step 1: Failing tests:** `test_admin_invites_an_accountant` (today's behaviour, spec decision A); `test_only_an_owner_gives_the_owner_role`; `test_last_owner_cannot_be_reassigned`; `test_role_string_alias_still_works`; `test_a_role_of_another_company_is_404`; `test_ordering_by_role_name`.
- [ ] **Step 2–4: Red, implement, green; full verification as in A6 Step 4.**
- [ ] **Step 5: Commit, open PR2.** CHANGELOG lists the new endpoints, error codes, the `role` → `company_role` change and the deprecated `role`/`role_key` aliases (removed in Part E).

---

# Part C — Web: permission-driven UI (PR3)

Work in `shiftlush-web`. Before starting: `npm run api:sync && npm run api:generate` against API main with Part B merged.

### Task C1: Session permissions and `useCan`

**Files:**
- Modify: `src/lib/session.tsx:16,77-106,189,229-237`
- Create: `src/lib/permissions.ts`
- Test: `src/lib/permissions.test.ts`

**Interfaces:**
- Produces: `type Action = "read" | "write" | "delete" | "receive"`; `useCan(resource: string, action: Action): boolean`; `useRole(): SessionRole | null` with `{id, name, systemKey, isOwner, assignedOnly, fieldStaff}`; `can(permissions, resource, action)` pure helper for non-hook code (`lib/*.ts`).
- Session refetches `/auth/me` on `window` focus and after any API 403 (hook into the API client's error path).
- Styleguide `setRole` becomes `setPermissions(preset)` with presets built from the five seed permission sets.

- [ ] **Step 1: Failing tests:** `can` true/false cases; owner-like full set; `refetches me after a 403` (mock client).
- [ ] **Step 2–4: Red, implement, green** — `npm test`.
- [ ] **Step 5: Commit** — `SL-<n>-feat: the session knows what the user may do`

### Task C2: Navigation by resource

**Files:**
- Modify: `src/components/layout/nav-config.ts` (62 role hits), `Sidebar.tsx`, `UserMenu.tsx`, `src/router.tsx`
- Test: `src/components/layout/nav-config.test.ts`

**Interfaces:**
- Nav item: replace `roles: Role[]` with `requires: { resource: string; action: Action }` (or an array, any-of); per-role label overrides become `labelWhen: { assignedOnly?: string }`. Start route: `fieldStaff` → today's technician start, otherwise today's office start.

- [ ] **Step 1: Failing test:** for each of the five seed permission presets, the visible nav keys equal a snapshot taken **from the current code before changing it** (write the snapshot first, from today's `roles` arrays).
- [ ] **Step 2–4: Red, implement, green.**
- [ ] **Step 5: Commit** — `SL-<n>-refactor: navigation follows permissions`

### Task C3: Screens and helpers off role names

**Files (from the role-name grep):** `src/lib/visit.ts`, `lib/reports.ts`, `lib/plan.ts`, `lib/faults.ts`, `lib/inspections.ts`, `lib/portal.ts`, `lib/billing.ts`, `screens/list-searches.ts`, `UserDetailScreen.tsx`, `ElevatorDetailScreen.tsx`, `InviteUserScreen.tsx`, `CustomerDetailScreen.tsx`, `ContractDetailScreen.tsx`, `VisitFormScreen.tsx`, `VisitDetailScreen.tsx`, `CustomerListScreen.tsx`, `PlanScreen.tsx`, `FaultDetailScreen.tsx`, `FaultCreateScreen.tsx`, `ContractListScreen.tsx`, `AuditLogListScreen.tsx`, `VisitListScreen.tsx`, `TechnicianPlanScreen.tsx`, `SettingsScreen.tsx`, `MyFaultsScreen.tsx`, `FaultBoardScreen.tsx`, `ElevatorListScreen.tsx`, `ElevatorDetailMobile.tsx`, `components/plan/PlanRestricted.tsx`
- Create: `scripts/check-no-role-names.mjs`, `package.json` script `lint:roles`

- [ ] **Step 1: Write `lint:roles`** — fails on `role === "…"`, `role !== "…"` and role-name string literals `"owner"|"admin"|"operations"|"technician"|"accountant"` in `src/` outside `src/styleguide/`, `src/api/generated.ts` and `messages/`. Run it: expect it to list the files above.
- [ ] **Step 2: Convert each file**: an action guard → `useCan(resource, action)` with the resource the API checks for that endpoint; "is the technician" → `role.assignedOnly` (what they see) or `role.fieldStaff` (assignable, mobile layout). Existing vitest suites for these libs keep passing with the presets from C1.
- [ ] **Step 3: Verify** — `npm run typecheck && npm test && npm run lint:roles && npm run build`, all green.
- [ ] **Step 4: Commit, open PR3** — `SL-<n>-refactor: screens follow permissions, not role names`

---

# Part D — Web: Settings → Roles (PR4)

### Task D1: Roles list and editor

**Files:**
- Create: `src/screens/RoleListScreen.tsx`, `src/screens/RoleEditScreen.tsx`, `src/components/roles/PermissionGrid.tsx`
- Modify: `src/router.tsx`, `src/components/layout/nav-config.ts` (Settings → Roles, `requires: {resource: "role", action: "read"}`), `src/api/queries.ts`, `messages/tr.json` (role names by `system_key`, resource labels by key, group titles, all new error codes)
- Test: `src/components/roles/PermissionGrid.test.tsx`

**Interfaces:**
- `PermissionGrid({ catalogue, value, onChange, editorPermissions, scoped })` — rows grouped by `catalogue[].group`; a cell absent when the resource lacks the action; disabled with tooltip when the editor lacks it; rows with `scopable: false` disabled when `scoped`. Ticking write or delete ticks read; unticking read unticks write and delete.

- [ ] **Step 1: Failing grid tests:** the four cell rules above, plus the read/write/delete coupling.
- [ ] **Step 2–4: Red, implement, green.** List screen: owner role pinned and locked, user counts, "Yalnızca atananlar" badge, duplicate and delete (show `ROLE_IN_USE` counts). Editor: name, description, the two switches (the `field_staff` off switch shows the 409 count in a confirm dialog — use the app's existing dialog component, `npm run test:dialogs` must pass), grid.
- [ ] **Step 5: Commit** — `SL-<n>-feat: the roles screen`

### Task D2: Role pickers for users and invitations

**Files:**
- Modify: `src/screens/InviteUserScreen.tsx`, `src/screens/UserDetailScreen.tsx`, `src/api/queries.ts`
- Test: extend the screens' existing tests or add `InviteUserScreen.test.tsx`

- [ ] **Step 1: Failing test:** picker lists the firm's roles from `role-list`, owner role only for an owner, sends `company_role`.
- [ ] **Step 2–4: Red, implement, green; full web verification as in C3 Step 3.**
- [ ] **Step 5: Commit, open PR4** — `SL-<n>-feat: pick any of the firm's roles for a user or an invitation`

---

# Part E — API: drop the aliases (PR5, one release after Part D is live)

### Task E1: Remove `role`/`role_key` aliases and the compat property

**Files:** `apps/users/models.py` (compat `role` property), `apps/users/api/v1/serializers.py`, `team_serializers.py`, `team_views.py`, `apps/users/models.py` `Role` enum (keep only as `SEED_KEYS` source or delete), tests that still use `role=` (switch to a `make_user(company, role_key=…)` helper in `tests/`), `CHANGELOG-API.md`, `openapi/v1.yaml`.

- [ ] **Step 1: Failing test:** `test_role_string_is_no_longer_accepted` (400 unexpected field).
- [ ] **Step 2–4: Red, remove, green; full verification as in A6 Step 4.**
- [ ] **Step 5: Commit, open PR5** — `SL-<n>-chore: drop the role string aliases`
