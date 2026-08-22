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
