# Visit signature, API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A maintenance visit records whether the building signed for it, who
signed and when, holds exactly one signature image, and shows that state on
every read.

**Architecture:** The signature fields are added to `MaintenanceVisit`. The
image is an `Attachment` with a new category `signature`. The
signature-specific rules live in one new module, `apps/maintenance/signatures.py`,
which the visit serializers and the attachment service both call, so the rules
aren't split across two apps. The read state (`signature_state`) is derived
in one place from the status plus an `Exists` annotation for the image.

**Tech Stack:** Django 5, DRF, django-filter, drf-spectacular, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-visit-signature-design.md`

## Global Constraints

- Branch `SL-173` in a worktree at `~/projects/shiftlush-api-SL-173`. Commits
  are titled `SL-173-<type>: …` in English, with no AI attribution lines.
- Error codes are UPPERCASE members of `core.error_codes.ErrorCode`. Field
  rules use `core.exceptions.field_error(field, code)` or DRF's
  `ErrorDetail(..., code="required")` for a missing field. State conflicts
  use `core.exceptions.Conflict(code)` (409).
- No Turkish characters in `apps/`, `core/` or `config/` `.py` files (CI).
- `make spec` regenerates `openapi/v1.yaml`, which is committed.
- Done means `make lint`, `make mypy`, `make check` and `make test` pass.
- The signature image: `image/png` only, at most `200 * 1024` bytes.
- `signer_name` is at most 100 characters.
- Roles: `manager`, `caretaker`, `staff`, `other`.
- Statuses: `signed`, `not_present`, `declined`.

## Review Focus

1. **The offline outbox retries the signature confirm with the same storage
   key.** It must return the first attachment, not `SIGNATURE_EXISTS`.
   Pinned by `test_a_retried_confirm_is_the_same_signature` (Task 2).
2. **A technician's PATCH can't reach signature fields,** because PATCH is
   owner/admin only. But an owner's PATCH that sends `signer_name` must be
   refused, not silently applied. Pinned by
   `test_signature_fields_are_read_only_after_create` (Task 1).
3. **A visit whose image arrives before the visit row commits** is impossible
   by flow, but a confirm for a visit that isn't `signed` must be refused at
   confirm too, not only at upload-url. Pinned by
   `test_confirm_refuses_a_signature_for_an_unsigned_visit` (Task 2).
4. **`signed_at` in the future** (a phone clock ahead) is replaced by the
   server's time. Pinned by `test_a_future_signed_at_takes_the_server_time`
   (Task 1).
5. **Portal and office agree on the state.** One function computes it, and
   both serializers call it. Pinned by
   `test_portal_and_office_read_the_same_state` (Task 3).

---

### Task 1: The record and its rules

**Files:**
- Modify: `apps/maintenance/models.py`. Add `SignatureStatus(TextChoices)`,
  `SignerRole(TextChoices)` and five fields on `MaintenanceVisit`:
  `signature_status` (CharField 20, blank), `signer_name` (100, blank),
  `signer_role` (20, blank), `signed_at` (DateTimeField null), and
  `amended_after_signature` (Boolean, default False). Add check constraints
  for the two choice sets, allowing blank.
- Create: migration `apps/maintenance/migrations/00xx_visit_signature.py`.
- Create: `apps/maintenance/signatures.py` with the rule functions below.
- Modify: `apps/maintenance/api/v1/serializers.py`, in
  `MaintenanceVisitWriteSerializer`: add the four input fields and call the
  rules from `validate()`; mark amendments in `update()`.
- Test: `tests/test_visit_signature.py` (new).

**Interfaces:**
- Produces, in `apps.maintenance.signatures`:
  - `check_signature_fields(status: str, attrs: dict[str, Any], *, creating: bool) -> dict[str, Any]`
    raises field errors and returns attrs, with `signed_at` normalised.
  - `SIGNATURE_FIELDS: tuple[str, ...] = ("signature_status", "signer_name", "signer_role", "signed_at")`.

- [ ] **Step 1: Write the failing tests** in `tests/test_visit_signature.py`.
  Reuse the visit-create fixtures from `tests/test_maintenance_visit_create.py`
  (import them), and POST with an `Idempotency-Key`.
  - `test_a_completed_visit_needs_a_signature_status`: completed with no
    `signature_status` → 400, details `[{"field": "signature_status", "code": "required"}]`.
  - `test_an_incomplete_visit_may_leave_it_blank`: incomplete with a
    `status_note` and no signature → 201, `signature_status == ""`.
  - `test_signed_needs_a_name_and_a_role`: `signed` with no name → 400 on
    `signer_name` `required`; with a name and no role → 400 on `signer_role`
    `required`.
  - `test_a_reason_carries_no_name`: `not_present` with `signer_name: "Ali"`
    → 400, `signer_name` code `invalid`.
  - `test_signed_is_stored_with_its_time`: `signed`, `signer_name` "Ayşe
    Yılmaz", `signer_role` `manager`, `signed_at` "2026-10-22T09:15:00+03:00"
    → 201. The fields read back, with `signed_at` that instant.
  - `test_a_future_signed_at_takes_the_server_time`: `signed_at` an hour
    ahead of now → stored within a few seconds of `timezone.now()`.
  - `test_a_missing_signed_at_takes_the_server_time`: signed without
    `signed_at` → stored ≈ now.
  - `test_signature_fields_are_read_only_after_create`: the owner PATCHes
    `{"signer_name": "X"}` on a signed visit → 400, `signer_name` code
    `read_only`. The name is unchanged.
  - `test_an_edit_after_signing_is_marked`: the owner PATCHes
    `{"work_notes": "fixed"}` on a signed visit → 200,
    `amended_after_signature` is True. A second PATCH keeps it True.
  - `test_an_edit_before_any_signature_is_not_marked`: the owner PATCHes an
    incomplete visit with a blank status → `amended_after_signature` False.

- [ ] **Step 2: Run them to confirm they fail**

  Run: `.venv/bin/python -m pytest tests/test_visit_signature.py -q -p no:cacheprovider`
  Expected: FAIL. The unknown fields are refused by `StrictMixin`, and the
  model has no fields.

- [ ] **Step 3: Implement.** Model fields and migration (`makemigrations
  maintenance -n visit_signature`). In `check_signature_fields`:
  - `completed` with no status on create → required.
  - `signed` requires a name (stripped) and a role.
  - any other status rejects a non-empty name or role with `invalid`.
  - `signed_at` is replaced by `timezone.now()` when missing or later than
    now, and cleared when the status isn't `signed`.

  In the write serializer, on update, any `SIGNATURE_FIELDS` key in the
  initial data raises a field error with code `read_only`. In `update()`, set
  `amended_after_signature=True` when the instance's `signature_status` is
  `signed`.

- [ ] **Step 4: Run them to confirm they pass**, plus the existing visit
  suites.

  Run: `.venv/bin/python -m pytest tests/test_visit_signature.py tests/test_maintenance_visit_create.py tests/test_maintenance_visits.py -q -p no:cacheprovider`
  Expected: PASS. Existing completed-visit tests that post no signature now
  fail. Rule: add `"signature_status": "not_present"` to their payload
  helpers, and record that in the ledger.

- [ ] **Step 5: Commit.**
  `SL-173-feat: a visit says whether the building signed for it, who and when`

### Task 2: The signature image

**Files:**
- Modify: `apps/attachments/models.py`. Add `SIGNATURE = "signature", "Signature"`
  to `AttachmentCategory`, plus its migration if the choices are constrained.
- Modify: `apps/maintenance/signatures.py`. Add:
  - `check_signature_upload(company_id: uuid.UUID, visit_id: uuid.UUID, mime_type: str, size_bytes: int) -> None`
  - `check_signature_confirm(company_id: uuid.UUID, visit_id: uuid.UUID, storage_key: str) -> None`
  - `MAX_SIGNATURE_BYTES = 200 * 1024`
- Modify: `apps/attachments/services.py`. `prepare_upload` and `confirm_upload`
  call these when `category == AttachmentCategory.SIGNATURE`, after their own
  checks, before issuing or recording.
- Modify: `apps/attachments/api/v1/permissions.py`. The technician carve-out
  for `maintenance_visit` accepts `PHOTO` and `SIGNATURE`.
- Modify: `apps/attachments/api/v1/views.py`. `perform_destroy` refuses a
  signature with `Conflict(ErrorCode.SIGNATURE_CANNOT_BE_DELETED)`.
- Modify: `core/error_codes.py`. Add `SIGNATURE_EXISTS`,
  `SIGNATURE_NOT_EXPECTED` and `SIGNATURE_CANNOT_BE_DELETED` to the conflict
  group.
- Test: `tests/test_visit_signature.py`, class `TestSignatureImage`, using
  the `bucket` fixture and `uploaded()` from `tests/test_attachments.py`.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_technician_uploads_the_signature_of_their_signed_visit`:
    upload-url plus confirm with category `signature` and `image/png` → 201.
  - `test_only_png_and_small`: `image/jpeg` → 422 `UNSUPPORTED_MIME_TYPE`;
    `size_bytes` 300000 → 422 `FILE_TOO_LARGE`.
  - `test_upload_url_refuses_a_signature_for_an_unsigned_visit`: on a
    `not_present` visit → 409 `SIGNATURE_NOT_EXPECTED`.
  - `test_confirm_refuses_a_signature_for_an_unsigned_visit`: get a ticket
    while signed, then flip the status to `declined` in the DB, then confirm
    → 409 `SIGNATURE_NOT_EXPECTED`.
  - `test_a_second_signature_is_refused`: two different keys → the second
    confirm is 409 `SIGNATURE_EXISTS`.
  - `test_a_retried_confirm_is_the_same_signature`: the same key twice → 201
    both times, same id, one row.
  - `test_nobody_deletes_a_signature`: the owner DELETEs it → 409
    `SIGNATURE_CANNOT_BE_DELETED`, and it still exists. A photo can still be
    deleted by the owner.

- [ ] **Step 2: Run them to confirm they fail.**
  Run: `.venv/bin/python -m pytest tests/test_visit_signature.py -k Image -q -p no:cacheprovider`
  Expected: FAIL. The category is invalid.

- [ ] **Step 3: Implement**, as the Files list says. `check_signature_upload`
  reads the visit with `MaintenanceVisit.unscoped.filter(company_id=…, pk=…)`,
  for the same reason `_target_exists` does. The PNG and size checks raise
  `BusinessRuleError` with the existing codes, so they read like every other
  upload refusal.

- [ ] **Step 4: Run them to confirm they pass**, plus the attachment suites.
  Run: `.venv/bin/python -m pytest tests/test_visit_signature.py tests/test_attachments.py tests/test_maintenance_attachments.py -q -p no:cacheprovider`
  Expected: PASS.

- [ ] **Step 5: Commit.**
  `SL-173-feat: one signature image per visit, never deleted`

### Task 3: Reading the state

**Files:**
- Modify: `apps/maintenance/signatures.py`. Add
  `signature_state(visit: MaintenanceVisit) -> str`, which returns
  `signed | pending | not_present | declined | none`, and
  `with_signature(queryset) -> QuerySet` (annotates `has_signature_image`
  with `Exists` over the visit's non-deleted signature attachments).
- Modify: `apps/maintenance/api/v1/serializers.py`.
  `MaintenanceVisitReadSerializer` gains `signature_status`, `signer_name`,
  `signer_role`, `signed_at`, `amended_after_signature` and
  `signature_state`.
- Modify: `apps/maintenance/api/v1/views.py`. The queryset goes through
  `with_signature`. `MaintenanceVisitFilter` gains
  `signature = ChoiceFilter(method=…)` with the five states.
- Modify: `apps/portal/api/v1/serializers.py` and `views.py`.
  `PortalVisitSerializer` gains `signature_state`, `signer_name`,
  `signer_role`, `signed_at`, `amended_after_signature` and `signature` (the
  image file or null). The portal's files prefetch also collects category
  `signature` and hands it in separately from `photos`.
- Test: `tests/test_visit_signature.py`, class `TestReading`.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_states`: one visit each of signed-with-image, signed-without,
    `not_present`, `declined` and blank → `signature_state` values `signed`,
    `pending`, `not_present`, `declined`, `none` on the list.
  - `test_the_list_filters_by_state`: `?signature=pending` returns only the
    signed-without-image visit, and so on for each value.
  - `test_the_list_has_no_n_plus_one`: listing 1 visit versus 5 visits takes
    the same number of queries (`django_assert_num_queries` around both, or
    compare `CaptureQueriesContext` counts).
  - `test_portal_and_office_read_the_same_state`: for the signed-with-image
    visit, the portal's visit and the office's visit carry the same
    `signature_state` and `signer_name`. The portal's `signature` has a URL,
    and `photos` doesn't include the signature.

- [ ] **Step 2: Run them to confirm they fail.**

- [ ] **Step 3: Implement**, as the Files list says. On a read without the
  annotation (a create response), `signature_state` checks the attachment
  once.

- [ ] **Step 4: Run them to confirm they pass**, plus the portal and visit
  suites.
  Run: `.venv/bin/python -m pytest tests/test_visit_signature.py tests/test_portal.py tests/test_maintenance_visits.py -q -p no:cacheprovider`

- [ ] **Step 5: Commit.**
  `SL-173-feat: every reader sees the same signature state`

### Task 4: The demo

**Files:**
- Modify: `core/storage.py`. Add
  `put(backend: str, key: str, data: bytes, content_type: str) -> None`
  through the same client as `stat` and `delete`.
- Modify: `core/demo_activity.py`. Add a step `_signatures(firm, rng)` after
  the visits, plus `Activity.signatures`. Add `signatures +N` to the
  `refresh_demo_data` output.
- Test: `tests/test_demo_activity.py`.

Behaviour: completed visits with a blank `signature_status` get a status:
about 80% `signed`, 15% `not_present` and 5% `declined`, seeded. A signed one
gets a name from the demo's resident names, a role, `signed_at` at the visit
day at 10:00–16:00 Istanbul time, and a PNG drawn with Pillow (a few random
bezier strokes on 600×240 white). The PNG is stored with `storage.put` and an
`Attachment` row of category `signature`. When `storage.reachable(backend)`
is False (local SQLite with no bucket), the visit becomes `not_present`
instead, so the demo never shows "pending". The step is idempotent per visit
(blank status only).

- [ ] **Step 1: Write the failing tests.** With the `bucket` fake patched in:
  after seeding, completed visits all have a status, at least one of each
  kind exists, and every `signed` one has exactly one signature attachment. A
  second refresh reports `signatures +0`. Without the fake (`reachable` →
  False), there is no `signed` visit and none is pending.
- [ ] **Step 2: Run them to confirm they fail.**
- [ ] **Step 3: Implement.** Pillow is already a dependency if the label PDFs
  use it. If it isn't, write the PNG with the standard library (`zlib` +
  `struct`) rather than adding a dependency. Record which in the ledger.
- [ ] **Step 4: Run them to confirm they pass.**
  Run: `.venv/bin/python -m pytest tests/test_demo_activity.py -q -p no:cacheprovider`
- [ ] **Step 5: Commit.**
  `SL-173-feat: the demo's visits are signed for, or say why not`

### Task 5: Contract, gate, PR

- [ ] Run `make spec`. The diff shows the visit fields, the filter, the
  attachment category and the three codes.
- [ ] Run `make lint && make mypy && make check && make test`. All pass.
- [ ] Add a pointer to this milestone in the M1 spec's "Non-goals" note on
  signatures, if there is one; otherwise skip.
- [ ] Commit `SL-173-chore: publish the signature's API contract`, push, and
  open a PR that closes the API issue. Note that the web must translate the
  three codes.
