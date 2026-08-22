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

### Changed

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
