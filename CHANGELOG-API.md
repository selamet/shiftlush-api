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
