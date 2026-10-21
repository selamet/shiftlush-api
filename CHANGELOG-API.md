# API changelog

What changed in the published contract, and whether a client has to do anything
about it. `openapi/v1.yaml` is generated, so it records *what* changed; this file
records *why*, and it is the only place a breaking change is allowed to be
announced.

**Rules.** Adding an endpoint, an optional request field, a response field or an
error code is backwards compatible. Removing or renaming any of them, making an
optional field required, or narrowing an enum is breaking and needs a new
version — a client switching on a code it no longer receives falls through to
nothing, and nothing is the one outcome that produces no bug report.

CI enforces the first half of this: `oasdiff` fails the build on a breaking
change, and the schema is regenerated and diffed on every commit so the contract
cannot drift from the code.

---

## v1 — unreleased

Phase 1. The contract is not frozen until the first production deployment;
until then breaking changes are made in place and recorded here rather than
carried as compatibility shims nobody will ever remove.

### Added

- **Authentication** — register, login, refresh, logout, password reset,
  e-mail verification, `/auth/me`. The refresh token lives in an httpOnly
  cookie and is rotated on every use; a replayed token revokes every session.
- **Company** — `/company`, a singleton with no id in the path. The only
  company a request can address is the one in its token.
- **Users and invitations** — `/users` (read and update only) and
  `/invitations`. Accounts are created by invitation; there is no endpoint that
  sets somebody else's password, and no endpoint that deletes a user.
- **Address lookup** — provinces, districts and neighbourhoods, read-only.
- **Reverse geocoding** — `GET /geocode/reverse?lat=&lng=`, so the map picker
  never calls a geocoder itself. It answers with the **ids** of the province,
  district and neighbourhood the point resolved to, a `confidence` for each, and
  an `unmatched` list naming the levels that did not resolve. Nothing from the
  provider's own payload is forwarded, which is what keeps the provider
  replaceable without a client change.

  A level is either matched or explicitly not: below the similarity threshold,
  on a tie between two equally good rows, or under a parent that did not match,
  the answer is `null` and a name in `unmatched` rather than a best guess. A
  client should treat that as "ask the user", not as "empty because nothing was
  found yet".

  Provider failures and timeouts answer `503 SERVICE_UNAVAILABLE` — an existing
  code, deliberately, so no client has to learn a new one for a case whose
  handling is the same as every other unavailable dependency. Exhausting the
  rate limit answers `429 THROTTLED`, which is the first endpoint in this API to
  do so.
- **Customers**, **complexes and buildings**, **elevators**, **contracts** —
  full CRUD with soft delete. Contract state changes are their own endpoints
  (`terminate`, `renew`) rather than a status field, because they touch several
  tables and have rules a client should not be trusted to apply.
- **QR** — `/elevators/{id}/regenerate-qr`, `/elevators/by-qr/{token}`, and
  `/elevators/labels`, which returns a printable A4 sheet of twelve labels.
- **Attachments** — `/attachments/upload-url`, `/attachments`,
  `/attachments/{id}/download-url`. File bytes never pass through the API; the
  client uploads straight to storage with a signed URL and confirms afterwards.
- **Audit log** — `/audit-logs`, read-only, owners and administrators only.
- **`Idempotency-Key`** on create endpoints. The same key returns the same
  response; the same key with a different body is refused with 409.
- **`ErrorCode` and `ErrorResponse`** components, generated from
  `core/error_codes.py`. Every code the API can return is in the contract, and
  the frontend's CI fails if one of them has no Turkish message.
- **Maintenance visits** — `POST/GET /maintenance-visits/`,
  `GET/PATCH/DELETE /maintenance-visits/{id}/`,
  `POST /maintenance-visits/{id}/set-checklist/`. Records a technician's
  routine maintenance visit against an elevator, with a 16-item checklist,
  and recalculates the elevator's `next_maintenance_date`. A technician may
  create their own visit and attach an optional photo to it, but only
  `OWNER`/`ADMIN` may edit or delete one afterwards. Elevators gain read-only
  `last_maintenance_date` and `next_maintenance_date`, and attachments accept
  `maintenance_visit` as an `object_type`.
- **Notifications** — the signed-in user's own, and nobody else's. Every role
  may use these; an owner cannot read a colleague's.
  - `GET /notifications/` — newest first, paginated, `?unread=true|false` and
    `?kind=`. Each row is `{id, kind, subject_type, subject_id, data, read_at,
    created_at}`.
  - **No sentence in the response.** The client renders the Turkish from
    `kind` (the new `NotificationKind` enum) and `data`, which holds the facts
    the sentence needs, captured when it was raised. Dates in `data` are ISO
    strings. `subject_type` (`contract`, `elevator`) and `subject_id` are for
    the link.
  - `GET /notifications/unread-count/` — `{count}`, for the bell.
  - `POST /notifications/{id}/read/` — idempotent; marking twice keeps the first
    `read_at`. Somebody else's id is `404 NOT_FOUND`, as for one that never
    existed. Answers with the notification.
  - `POST /notifications/read-all/` — answers `{count}`, how many changed.
  - `GET /notifications/preferences/` — every kind with its `email` flag,
    defaults filled in: on for the four contract kinds, off for
    `maintenance_overdue`. In-app delivery is always on.
  - `PUT /notifications/preferences/` — the same list back. It replaces the
    flags: a kind left out returns to its default, and an unknown kind or the
    same kind twice is `400 VALIDATION_ERROR`.
  - Nothing here creates a record, so none of it takes `Idempotency-Key`; both
    POSTs converge on a retry by themselves.
  - Only the server raises notifications (the M2 contract jobs). An e-mail copy
    goes out after the job commits, when the recipient's preference allows it.
- **Maintenance plan** (M3) — one `planned-visit` per lift under an active
  contract per month, created by a daily job for this month and next, and
  closed by the visit that does the work. Owners, admins and operations
  arrange it; technicians read it; accountants have no access.
  - `GET /planned-visits/?period=YYYY-MM` — `period` is required. Filters:
    `technician` (an id, or `me`), `unassigned`, `status`, `due_before`
    (inclusive), `district`, `customer`, `elevator`. Route order by default
    (district, neighbourhood, building, lift); `?ordering=due_date` too. Each
    row carries the elevator, building, neighbourhood, district and customer
    names, `technician_name`, `is_overdue`, and `latest_visit_status` — the
    status of the linked visit. `planned` with a `latest_visit_status` means
    tried, not done.
  - A technician sees their own rows and the unassigned rows of the customers
    they are assigned to. Another technician's row is `404`.
  - `GET /planned-visits/{id}/` — one row, found by id alone.
  - `PATCH /planned-visits/{id}/` — `technician` (an active user of your firm,
    or `null`) and `planned_date`. Sets `manually_edited`, after which the daily
    job never changes or cancels the row.
  - `POST /planned-visits/bulk-assign/` — `{ids, technician}`, all or nothing:
    one id that is not yours refuses the request with `400` on `ids`, and
    nothing changes. Answers `{count}`. No `Idempotency-Key`; a retry converges.
  - `GET /planned-visits/summary/?period=` — `total`, `by_status`,
    `unassigned`, `overdue`, and per technician `planned`, `done`,
    `cancelled`, `overdue`. The list filters apply.
  - There is no create and no delete: the rows are the server's.
  - **Recording a visit closes the row** for that lift and month: `completed`
    makes it `done`; `incomplete` and `access_denied` leave it `planned` with
    the attempt linked. Deleting the visit reopens the row, and moving its date
    to another month moves the link. Nothing changes in the visit endpoints'
    requests or responses.
  - New notification kind **`visit_assigned`**, `{period, count}`, with
    `subject_type: "plan"` and the recipient's id as `subject_id`. One per
    technician, month and assignment action (the daily job, a PATCH, a bulk
    assignment), never one per row. E-mailed by default.
  - New enum `PlannedVisitStatus`: `planned`, `done`, `cancelled`.
- **Faults** (M4) — from the first report to the closed job.
  - `GET/POST /faults/`, `GET/PATCH /faults/{id}/`. Filters: `status`,
    `priority`, `channel`, `technician`, `elevator`, `customer`,
    `reported_at_after`/`reported_at_before`. `?ordering=priority_rank,reported_at`
    is the board: emergencies first, then the oldest. Each row carries the
    elevator, building and customer names, `reference` (six characters, for the
    phone), every timestamp, and the derived `response_time_seconds` and
    `resolution_time_seconds`.
  - `POST /faults/` — owner, admin, operations and technician. Starts `open`,
    or `assigned` with a `technician`. `someone_trapped: true` makes it
    `emergency` whatever `priority` says. `stopped_elevator: true` puts an
    `active` lift `out_of_service`. A technician reports only on their
    customers' lifts and may name only themselves. Replay-protected like every
    create.
  - Transitions, each its own endpoint so no timestamp is the client's:
    `assign` (office; also reassigns), `arrive` and `resolve` (the assigned
    technician, or the office), `close` and `cancel` (office; `cancel` needs a
    `reason`). From the wrong state each answers `409
    FAULT_TRANSITION_NOT_ALLOWED`, a new code. `resolve` takes
    `resolution_notes`, `parts_used` and `restore_service`, which puts an
    `out_of_service` lift back to `active`.
  - `billable` is derived at resolve from the contract that covered the lift on
    the day of the report: maintenance only → yes, maintenance and repair →
    only with `parts_used`, full coverage → no, none → yes. `PATCH` with
    `billable` overrides it and sets `billable_overridden`; resolve then keeps
    the office's answer.
  - `PATCH` is the office's: `priority`, `description`, `billable`. No
    `DELETE`; a mistaken fault is cancelled.
  - Accountants have no access. Technicians see the faults of their customers.
  - New enums `FaultStatus`, `FaultPriority`, `FaultChannel`.
- **`POST /public/elevators/{qr_token}/faults/`** — a resident's report from
  the QR sticker, with no account. The only unauthenticated write.
  - Body: `description`, `someone_trapped`, `elevator_stopped`, and optional
    `reporter_name` and `reporter_phone`. Answers `202 {"reference"}` and
    nothing else; an unknown sticker is `404`.
  - Priority comes only from what the resident says: trapped → `emergency`,
    stopped → `high`, otherwise `normal`.
  - A report on a lift that has an open or assigned public report from the
    last 30 minutes is added to it, and answers with its reference.
  - Throttled per sticker (5/hour) and per address (20/hour); `429 THROTTLED`
    with the usual `RateLimit-*` headers.
  - No `Idempotency-Key`: there is no user to key it by, and the merge absorbs
    a retry.
- **Attachments** accept `object_type: "fault"`. A technician may attach photos
  to a fault assigned to them.
- **Notifications:** new kinds `fault_reported`, `fault_emergency`,
  `fault_assigned`, `fault_resolved`, linking to the fault
  (`subject_type: "fault"`). `fault_emergency` is always e-mailed: the
  preference list marks it `locked: true` (a new read-only field on every
  entry), and turning it off is `400` with the new code
  `NOTIFICATION_EMAIL_LOCKED`.
- **Periodic inspections** (M5) — what the accredited inspection body found,
  and what is due before the municipality seals a lift.
  - `GET/POST /inspections/`, `GET/PATCH /inspections/{id}/`. Write: owner,
    admin, operations. Read: those, and technicians for their customers' lifts.
    Accountants have no access. No `DELETE`. Filters: `elevator`, `label`,
    `kind`, `due_before` (next inspection, inclusive), `correction_before`
    (inclusive), `has_open_nonconformities`.
  - `POST` takes `elevator`, `kind` (`periodic` or `follow_up`, which needs
    `follows`), `inspected_on`, `inspection_body`, `report_number`, `label`,
    an optional `next_inspection_due` (a year on by default; a follow-up keeps
    the cycle of the inspection it follows) and an optional `nonconformities`
    list. `next_inspection_due` must fall after `inspected_on`
    (`END_DATE_BEFORE_START_DATE`, the code the elevator form used to give).
  - The read side carries `correction_deadline` (derived from the label:
    yellow 120 days, red 60, blue by the next inspection, green none — a
    follow-up counts from the original inspection), `days_left`,
    `lift_usable`, `sealed_if_missed` and the `nonconformities`.
  - `PATCH` corrects an inspection, and links its report PDF with `report`:
    an attachment of this inspection (`object_type: "inspection"`, new) filed
    as `inspection_report`. Anything else is `422 ATTACHMENT_TARGET_MISMATCH`.
  - Recording one updates the elevator's five inspection fields from the
    latest inspection. A red label puts an `active` lift `out_of_service`; a
    later non-red result puts it back.
  - `POST /inspections/{id}/nonconformities/`, `GET/PATCH /nonconformities/{id}/`
    (`status`, `fixed_on`, `description`; fixed with no date means today).
  - `POST /nonconformities/{id}/open-fault/` opens an M4 fault for the defect,
    `high` for red and `normal` otherwise, and links it in `fault`. `201`; `200`
    with the existing fault if it already has one.
  - `GET /inspections/due/?days=` — inspections and open correction deadlines
    in the window (default 60 days), overdue ones included, ordered by date.
  - New notification kinds `inspection_due`, `correction_deadline_near`,
    `correction_deadline_passed` and `inspection_red_label`; the last two are
    always e-mailed and listed as `locked`.
  - New enums `InspectionResultLabel`, `InspectionKind`, `NonConformityStatus`,
    `InspectionDueType`.

### Changed

- **`PATCH /inspections/{id}/` and `PATCH /nonconformities/{id}/` are
  described with the fields they take.** The inspection edit was described
  with the create body, and the non-conformity edit with no body at all.
  Nothing on the wire changed.

- **`PATCH /faults/{id}/` is described with the fields it takes**
  (`priority`, `description`, `billable`). The contract used to describe the
  create body. Nothing on the wire changed.

- **Breaking: the elevator's inspection fields are read-only.**
  - `last_inspection_date`, `inspection_label`, `next_inspection_date`,
    `inspection_body` and `inspection_report_number` are no longer accepted by
    `POST` and `PATCH /elevators/`. Sending one is `400`, as for any unknown
    field. They are still returned.
  - The latest recorded inspection writes them, so the elevator cannot
    disagree with what the inspector said. Record an inspection instead.
  - Made in place, because v1 is unreleased and the only client
    (`shiftlush-web`) is changing its elevator form in the same milestone.
- **The notification kind enum grew** (four inspection kinds). A client that
  switches on `kind` must handle values it does not know; `oasdiff` reports
  each addition to the preference list as a breaking response change.
- **Foreign-key filters work.** `?elevator=`, `?customer=`, `?building=` and
  every other filter on a firm-owned record were answered with
  `400 VALIDATION_ERROR` (`invalid_choice`) for any id, including the
  elevator page's maintenance history. They now filter. Another firm's id
  still matches nothing.

- **Create and edit responses are described as what they return.**
  - Every create (`POST`) and edit (`PATCH`) on the business endpoints has
    always answered with the record as it is read: `id`, names, derived
    fields.
  - The contract described those responses as the request body echoed back.
    That was 6 creates and 8 edits (buildings, complexes, contracts, customer
    contacts, customers, elevators, maintenance visits, planned visits).
  - It now names the read schema, and the unused `…Write` response components
    are gone. Request bodies are unchanged.
  - Nothing on the wire changed, only the description of it. A generated client
    gains the fields it was already receiving.

- **`POST /maintenance-visits/` replays a retried request for 30 days, not 24
  hours.** A phone can hold a visit in its outbox for days. If the first send
  landed and only the response was lost, a retry on day three now returns the
  same visit instead of recording it again. Every other create keeps the
  24-hour window.

- **Contracts now end on their own.** A daily job (00:30 Istanbul) closes every
  `ACTIVE` contract whose `end_date` has passed:
  - **Without `auto_renew`:** it becomes `expired`, and its elevators become
    `uncontracted`.
  - **With `auto_renew`:** it becomes `renewed`, and an `active` successor
    carries the same terms and elevators into the next term.
  - **With `auto_renew` but no VAT rate:** the successor stays a `draft`.
  - Clients that never saw `expired`, or saw `renewed` only after a manual
    renewal, will now see both without anyone acting. Each change raises a
    notification (`contract_expired`, `contract_auto_renewed` or
    `contract_auto_renewal_blocked`).
- **Two daily notices:**
  - `contract_renewal_due`, `renewal_notice_days` before a contract ends.
  - `maintenance_overdue`, once an active-contract elevator's
    `next_maintenance_date` has passed.

- **What the visit screens needed** (all backwards compatible):
  - **Elevator detail** returns `last_maintenance_date` and
    `next_maintenance_date`. Both are read-only and derived from completed
    visits.
  - **`GET /maintenance-visits/technicians/`** returns the firm's active
    technicians as `{id, name}`, for the "who did the work" field. It is open to
    anyone who may read visits. The user list stays owners' and admins' only.
  - **`GET /maintenance-visits/`** filters by `visit_date_after` and
    `visit_date_before`, both inclusive.
  - **The contract** now describes the create response as `MaintenanceVisitRead`,
    which is what the view always returned. It used to describe the request body
    without an `id`. Edit responses are described the same way.
- **Breaking: `POST /maintenance-visits/` requires an `Idempotency-Key`
  header.**
  - Without it the request is `400 IDEMPOTENCY_KEY_REQUIRED`, and nothing is
    recorded.
  - This is the endpoint a phone retries from its outbox on a bad connection
    (spec 8.10), so a missing key would record a retried visit twice.
  - Generate the key once per visit form and reuse it on every retry.
  - Made in place, because the only client (`shiftlush-web`) creates no visits
    yet. The screen that will is the first to send it.
- **`POST` and `PATCH /maintenance-visits/` accept `items`.**
  - Same shape as `set-checklist`.
  - Applied in the same transaction as the visit, so one request records the
    whole form.
  - Sending `items` replaces the checklist; leaving it out keeps it.
  - The same item twice, or an unknown item, refuses the whole request.
  - Creating and editing a visit is now atomic: the visit, its checklist and
    the elevator's maintenance dates commit together or not at all.
- **`POST /maintenance-visits/` — `technician` must be an active user of your
  own firm.**
  - Before, any user id was accepted, including one from another firm, and the
    response then read back that user's name. Such an id is now
    `400 VALIDATION_ERROR`, reported on `technician` with the code
    `does_not_exist` — the same answer as an id that was never issued.
  - A technician may leave `technician` out, and the visit is recorded as theirs.
  - A technician who names somebody else gets `TECHNICIAN_RECORDS_OWN_VISITS`
    on the `technician` field.
  - Everyone else still has to send the field.
  - The same scoping applies to `PATCH`.
- **Every endpoint is now rate limited, so `429 THROTTLED` is reachable
  API-wide.** Twenty requests a minute per address without a token, three
  hundred a minute per user with one (§8.13). Until now only
  `/geocode/reverse` could answer 429; a client that treated it as a
  geocoding-only case has to handle it everywhere.

  Nothing in the contract changes shape: no request or response field moves, no
  error code is added — `THROTTLED` has been in the vocabulary since the first
  draft. What changes is which endpoints can produce it, which is why it is
  recorded here rather than left to the schema diff, where it does not appear
  at all.

  The anonymous limit is per address and applies to the endpoints that admit
  anonymous callers — login, refresh, registration, password reset. It is
  shared by everyone behind one office address, and it is deliberately low:
  twenty login attempts a minute from one address is not an honest client. The
  authenticated limit is per user, so an office of twenty people does not share
  one allowance, and a technician moving between wifi and mobile data does not
  get a fresh one on each network.

  `/geocode/reverse` keeps its own, smaller budget and is not counted twice.
  `/health` and `/ready` are not counted at all.

  **What a client should do.** Read `RateLimit-Remaining` and slow down before
  it reaches zero; on a 429, wait the number of seconds in `Retry-After` rather
  than retrying immediately. A client that retries a 429 straight away spends
  its next window on refusals and never recovers.

- **`POST /customers` now requires the identifier that matches the type.** An
  `individual` needs `national_id` and may not carry `tax_number` or
  `tax_office`; the four organisation types need `tax_number` and may not carry
  `national_id`. Refused with 400 and a per-field code —
  `FIELD_REQUIRED_FOR_CUSTOMER_TYPE` or `FIELD_NOT_VALID_FOR_CUSTOMER_TYPE`.

  Breaking: a field that was optional is now required. Made in place because v1
  is unreleased. The schema still lists both as optional, because OpenAPI cannot
  express "required depending on the value of another field" without splitting
  the model into a union — so a client that omits one gets a 400 rather than a
  type error, and the form has to show it.

  `PATCH` is deliberately narrower: the requirement is checked when the request
  carries the field or changes the type, never otherwise. A customer created
  before this change can still have its notes edited; it cannot have its
  identifier cleared, and it cannot be moved to a type it does not fit.

- **A tax number or national ID is unique within a company.** A second customer
  with one already in use is refused with 409 `DUPLICATE_TAX_NUMBER` or
  `DUPLICATE_NATIONAL_ID`. Soft-deleted customers release theirs, and two
  different companies may of course hold the same one.

- **`?search=` on `/customers` folds Turkish.** `sisli`, `şişli` and `ŞİŞLİ` all
  find `Şişli Site Yönetimi`; before, none of them did. No request or response
  field changes — the same query now returns the rows it always should have.

- **Marking a second contact primary succeeds** and moves the flag off the
  previous one, rather than answering 500. No client change is needed; a client
  that worked around it by clearing the old flag first can stop.

- **`POST /contracts` now requires `vat_rate`, and a contract with no rate no
  longer has a total.** Three things that used to be one number are now three
  different answers.

  Elevator maintenance is VAT-liable in Turkey, so a blank rate is an omission
  far more often than a decision — and the decision already had a way to say
  itself, `0.00`. A missing rate used to be read as zero, which produced a
  `monthly_total` that looked complete and was short by the VAT. Nobody
  re-reads a number that filled itself in; it surfaced at reconciliation months
  later, across every invoice raised from that contract.

  - `vat_rate` is **required on create** and may never be `null`. Breaking: an
    optional request field became required. Made in place because v1 is
    unreleased. `PATCH` stays partial — an existing contract can have its notes
    edited without restating its rate — but `"vat_rate": null` is a 400, so a
    rate cannot be cleared once it has been stated.
  - `vat_rate` must be between `0` and `100`. Refused with 400 on the field.
    The bound is not in the schema, because a money value crosses the wire as a
    string and OpenAPI cannot put a numeric range on one; the form has to show
    the 400. `2000` typed for 20% fit `decimal(5, 2)` and invoiced twenty times
    the agreed amount.
  - **`vat_amount` and `monthly_total` are now nullable.** They are `null` when
    no rate was ever stated. `monthly_subtotal` is still answered — that part is
    known. This is a behaviour change as well as a document one: those two
    fields used to answer `"0.00"` and the subtotal.
  - **`vat_status`** is new on `ContractRead`: `applied`, `zero_rated` or
    `unset`. A screen switches on it rather than inferring the case from a null,
    which is what a hint reading "VAT is not calculated" was doing by guesswork.
    Like every other enum here it is a code; the Turkish belongs to the
    translation file.

  A contract can still legitimately have no rate — `POST /contracts/{id}/renew`
  with `copy_terms: false` produces a draft whose terms are still being
  negotiated. That is why the column stayed nullable, and it is exactly the
  state `unset` was added to describe.

  Regenerate the client. TypeScript will point at every place that read
  `monthly_total` as always present, which is the list of screens that would
  have shown the wrong figure.

### Added since the first draft

- **`neighborhood_name`, `district_name` and `province_name` on `CustomerRead`.**
  The billing address (§5.6) could be written and not read: the response carried
  an id, which no screen can display and no edit form can open a picker on.
  Buildings and complexes already returned all three.
- **`GET, POST /customers/{id}/contacts`** — specification §8.6, previously
  routed only as the flat `/customer-contacts`. The customer comes from the
  path; naming it in the body is a 400. `Idempotency-Key` is honoured.
- **`CONSTRAINT_VIOLATION`**, 409. A last resort for a database constraint that
  reaches the client without a rule of its own having caught it first. It
  replaces the `INTERNAL_ERROR` such a case used to produce, which told clients
  to retry something that could never succeed.
- **`POST /auth/password`** — change a password from inside a live session. Takes
  `current_password` and `new_password`, and answers with the same
  `TokenResponse` sign-in does. Backwards compatible: a new endpoint, no existing
  one changes. No new error code — a wrong current password is `422
  INVALID_CREDENTIALS`, deliberately distinct from the `400 VALIDATION_ERROR`
  that a password failing the policy gives, because the screen puts those two
  messages under different fields.

  Until now the only route to a new password was the reset link, which is the
  flow for somebody locked out and which ends every session — including the one
  the person was using. The settings screen had to ship a reset-link button
  behind a confirmation naming that consequence; it can stop.

  **Sessions on a voluntary change: every other session ends, the caller's does
  not.** §7.3 ends all sessions on a *reset*, and that is right there — a reset
  is completed by whoever holds the mailbox, and nothing in it proves the person
  at the keyboard is the one already signed in. A voluntary change is the
  opposite on both counts: possession of the current password was just proved, so
  the caller's session is the one known not to be the problem, and it is the
  session in their hand. Every other session is exactly what somebody changing
  their password wants gone.

  A client should treat the response the way it treats a sign-in: replace the
  access token it holds in memory. The refresh cookie is replaced too — the
  caller's old refresh token is revoked with all the others, so a change also
  retires it if it had leaked. The access token already issued stays valid until
  it expires, at most fifteen minutes, which is inherent to a stateless JWT.

  Rate limited per user, `429 THROTTLED` on exhaustion. Unthrottled the endpoint
  answers guesses about a secret.

- **`GET /auth/sessions`, `DELETE /auth/sessions/{id}`, `POST
  /auth/sessions/revoke-others`** — see and end the sessions on your own account.
  Somebody who signed in on a phone they no longer have had no way to end that
  session, or to know it existed.

  **One entry per device, not per refresh token.** Refresh rotation writes a new
  row every fifteen minutes, so a listing built on rows would show one browser as
  three thousand devices in a month. `id` names the session, is stable for its
  whole life, and is what `DELETE` takes — a row id would often have rotated away
  between the client rendering the list and the user clicking. Each entry carries
  `signed_in_at` (when the sign-in happened, not the last rotation),
  `last_used_at`, `expires_at`, `user_agent`, `ip_address` and `is_current`.

  `is_current` is answered from the refresh cookie, which is why these live under
  `/auth`: the cookie is path-scoped there, and the access token says who is
  calling but never from which device. Exactly one entry is current, unless the
  request carried no usable refresh cookie — then none is, and
  `revoke-others` ends everything including the caller's own session, because
  "all but this one" has no meaning when this one cannot be named.

  **Scoped to the caller and to nobody else.** There is no parameter that widens
  it and no role that does either: an owner administering the firm can deactivate
  a leaver, which ends that person's sessions, but cannot read which devices a
  colleague carries or pick one off. Another user's session id answers `404`, the
  same as an id that never existed — a `403` would confirm the id names a real
  session.

  The list is a plain array, the one list endpoint in this API without the
  pagination envelope. The collection is bounded by the number of devices one
  person is signed in on.

### Changed since the first draft

- **A revoked refresh token no longer always revokes every session.** Replaying a
  token that rotation superseded still does, unchanged: that chain has a live
  successor, two parties hold tokens for one live session, and there is no way to
  tell which is which. A token from a session somebody deliberately *ended* is
  now simply refused with `422 TOKEN_INVALID`.

  Without the distinction the sessions endpoints could not work: the evicted
  phone refreshes fifteen minutes later, that read as a replay, and ending the
  old device signed the user out of the laptop in their hand. Nothing is
  weakened — a token from an ended session cannot be exchanged for anything, so
  the blanket revocation it used to trigger was taking out sessions the replay
  had never reached.

### Corrected

- **Address names behind a nullable key were declared as always present.**
  `customer`, `building` and `complex` may each be entered before anyone knows
  where they are, and `neighborhood_name`, `district_name` and `province_name`
  are `null` on those rows. The contract said `string`, so a generated client
  read them as always there and the screen printed an empty line without
  anything having failed.

  Not a behaviour change: no response changes shape. Breaking against the
  document only, the same class of thing as the pagination envelope below.
  Regenerate the client; the three fields become nullable and TypeScript will
  point at the places that assumed otherwise.

- **Pagination envelope.** The contract declared DRF's default
  `{count, next, previous, results}` on every list endpoint. The server has
  always sent `{results, pagination: {page, page_size, total, total_pages}}` —
  `StandardPagination` overrode `get_paginated_response` but not
  `get_paginated_response_schema`, and the generator reads the second.

  Not a behaviour change: no server response changes shape, and no client can
  have depended on the documented fields because they were never sent. It is
  breaking against the *document*, which is why it is recorded here rather than
  in the added list. A client generated from the old contract read `count` as
  undefined and rendered a row count of zero — the types compiled and the build
  passed, which is why it survived this long.

  Regenerate the client. There is now a test that compares a real list response
  against the schema, so this class of drift fails the build rather than
  reaching a screen.

### Notes for clients

- Every response body is the resource itself. There is no success envelope: the
  HTTP status carries that, so no client has to parse a body to find out whether
  a call worked.
- Errors are always `{"error": {"code", "request_id", "details?"}}`. There is no
  `message` field — the words belong to the client's translation file.
- Money crosses the wire as a string. JavaScript's float arithmetic never
  touches a contract value.
- Timestamps are UTC, always. Localisation to Europe/Istanbul happens in the
  browser.
- Ids are UUIDv7 and are the only way to address a record. Nothing sequential is
  ever exposed, so nobody can infer how many customers a competitor has.
- Every response carries `RateLimit-Limit`, `RateLimit-Remaining` and
  `RateLimit-Reset`; a 429 carries `Retry-After` as well. `Reset` and
  `Retry-After` are both seconds, and both mean "when one more request is
  allowed" — at zero remaining they are the same number. All four are listed in
  `Access-Control-Expose-Headers`, so a browser client can actually read them.
