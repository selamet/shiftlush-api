# M4 — Fault management

Date: 2026-10-20. Roadmap: M4 (`2026-10-20-phase-2-roadmap.md`, #89).
Builds on M1 (visits and the phone flow), M2 (notifications) and M3 (the
technician's day).

## Why

When a lift stops, a firm's day reorganises around it, and today the product
plays no part. The call comes to somebody's phone. Who was sent and when is in
somebody's head. Whether the repair is billed depends on someone remembering
what the contract covers. And a trapped passenger looks exactly like a
flickering light.

M4 records the fault from the first report to the closed job:
- who reported it
- how urgent it is
- who was sent, and when they got there
- what they did
- whether it is billable

## The fault — `apps.faults`

### `Fault`

| Field | Meaning |
|---|---|
| `company`, `elevator` | Tenant-scoped. |
| `channel` | `staff` (entered by a firm user) or `public_qr` (the sticker). `customer_portal` arrives with M6. |
| `reported_by` | Nullable FK to User, for `staff`. |
| `reporter_name`, `reporter_phone` | Optional, for `public_qr`. This is personal data, so see Retention. |
| `description` | Required, at most 1,000 characters. |
| `priority` | `emergency` (trapped passenger), `high` (lift stopped) or `normal`. |
| `status` | `open` → `assigned` → `on_site` → `resolved` → `closed`; `cancelled` is reachable from any state before `resolved`. |
| `technician` | Nullable, scoped to the firm's active users, as SL-124 did for visits. |
| `reported_at`, `assigned_at`, `on_site_at`, `resolved_at`, `closed_at` | Set by the transitions, never by a client. |
| `resolution_notes`, `parts_used` | Text, at resolve. |
| `billable` | Derived at resolve (below). |
| `billable_overridden` | Set when the office overrides it. |
| `stopped_elevator` | The lift was out of service because of this fault. |

- The read side derives **response time** (`on_site_at - reported_at`) and
  **resolution time** (`resolved_at - reported_at`).
- Photos use the existing attachment flow with a new `object_type=fault`.
- The technician carve-out extends from visit photos to the photos of a
  fault assigned to them.

### Transitions

Each transition is an action, so the timestamps cannot be forged:

| Action | Who | Effect |
|---|---|---|
| `POST /faults/` | owner, admin, operations, technician | Creates the fault, in `open` or, with `technician`, in `assigned`. With `stopped_elevator`, an `active` lift becomes `out_of_service`. A trapped passenger means `priority=emergency`. |
| `POST /faults/{id}/assign/` | office | Sets `technician` and `assigned_at`; also used to reassign. |
| `POST /faults/{id}/arrive/` | the assigned technician, or the office | `on_site`, `on_site_at`. |
| `POST /faults/{id}/resolve/` | the assigned technician, or the office | Takes `resolution_notes`, `parts_used` and `restore_service`. Sets `resolved_at` and derives `billable`. With `restore_service`, an `out_of_service` lift returns to `active`. |
| `POST /faults/{id}/close/` | office | `closed`. The office has checked the work, and billing (M7) reads only closed faults. |
| `POST /faults/{id}/cancel/` | office | Takes a required `reason`. A duplicate report, for example. |
| `PATCH /faults/{id}/` | office | `priority`, `description`, and `billable` (sets `billable_overridden`). The audit trail records it, as it records every change. |

Every transition is atomic, and each one refuses to run from the wrong state:
`409` with `FAULT_TRANSITION_NOT_ALLOWED`.

### Billable

This is derived at resolve from the contract covering the lift on
`reported_at`:

| `Contract.scope` | Billable |
|---|---|
| `maintenance_only` | yes. Repairs are not covered. |
| `maintenance_and_repair` | only if `parts_used` is filled in. Labour is covered, parts are not. |
| `full_coverage` | no |
| No active contract | yes |

The office can override it. The override is audited and shown as
"elle değiştirildi".

## Public report from the QR sticker

The sticker URL is `/q/<token>`. Today that page needs a session. An
anonymous visitor (a resident standing at the lift) will instead get a
one-screen form: what is wrong, whether someone is trapped, and an optional
name and phone.

`POST /public/elevators/{qr_token}/faults/` is **the product's only
unauthenticated write**, so its threat model is part of the design:

- **Abuse.**
  - It is throttled twice: per token (`5/hour`) and per address (`20/hour`),
    as scoped throttles reporting their quota like every other limit.
  - A honeypot field that a person never fills in silently drops bots: they
    get the same `202` and nothing is stored.
  - There is no captcha. It would be a third-party script on a page people use
    in an emergency.
- **Disclosure.**
  - The response is `202 {"reference": "<6-char code>"}` and **nothing about
    the firm, the building or the lift**.
  - An unknown token gets `404`. The sticker is public already, so this reveals
    nothing new, and it tells a resident with a damaged sticker to call
    instead.
- **Flooding.** A public report on a lift that already has an `open` or
  `assigned` public report from the last 30 minutes is **attached to it** as
  an additional note instead of creating a new fault. Ten residents tapping
  the sticker make one fault, not ten. The reference returned is the existing
  fault's.
- **Priority.**
  - "Someone is trapped" gives `emergency`. It is not a field the public sets
    any other way.
  - Otherwise the report is `high` if the resident says the lift is stopped,
    and `normal` if not.
- **Retention.** The reporter's name and phone are kept until the fault is
  closed plus 90 days. A daily job (M0) then blanks them. Nothing else from a
  public report is personal.

## Notifications (M2)

| Kind | Who | E-mail default |
|---|---|---|
| `fault_reported` | owner, admin, operations, and the technicians assigned to the customer | on |
| `fault_emergency` | the same people, plus the assigned technician | **always e-mailed**: a trapped passenger is not a preference |
| `fault_assigned` | the technician | on |
| `fault_resolved` | owner, admin, operations | off |

Dedupe is per (fault, kind), and per assignment for `fault_assigned`.

## Web (separate PRs)

- **`/faults` board (office):** open faults by priority, then age; filters for
  status, technician and customer; emergency rows unmistakable, not by colour
  alone; response time as it runs.
- **Fault detail:** a timeline of reported, assigned, on site and resolved,
  with who and when; the actions for the viewer's role; photos;
  billable, with the office override.
- **Technician, mobile:**
  - "Arızalarım" (assigned to me) sits beside "Bugün" (M3).
  - "Yola çıktım / Vardım / Çözüldü" buttons at gloved-hand height. "Çözüldü"
    asks what was done, the parts, and whether to put the lift back into
    service.
  - Photos from the camera.
- **"Arıza bildir"** on the elevator detail, for staff.
- **Public report page** at `/q/$token` without a session. The current
  scan-to-elevator flow stays for signed-in users.
- Turkish strings for the new notification kinds and error codes.

## Non-goals

- A parts catalogue or stock (text plus quantity is enough).
- Invoicing. M7 reads closed faults.
- SMS or calling the technician.
- Customer-portal reporting. M6 reuses this API with `channel=customer_portal`.

## Decisions made without the user

- The state machine, with `close` as an office step after `resolve`.
- The billable rules per scope, including `maintenance_and_repair` meaning
  "parts billable".
- Public throttle numbers (5/hour per token, 20/hour per address), the
  30-minute merge of repeated public reports, and no captcha.
- Emergency e-mail ignores preferences.
- Public reporter data is kept until 90 days after closing.

## Slices

1. **Backend:** the faults app, transitions, billable, attachments, the public
   endpoint, notifications, and the retention job.
2. **Web:** the board, the detail, the technician mobile flow, and staff
   reporting.
3. **Web:** the public report page.
