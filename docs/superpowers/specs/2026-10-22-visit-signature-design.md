# A signature on the maintenance visit

Date: 2026-10-22. Phase 3. Builds on M1 (the maintenance visit and its offline
outbox) and M6 (the customer portal).

Written with defaults, at the user's request: they will review it before
anything is built. Every decision below is a default unless it says otherwise.

## Why

A maintenance visit today is the firm's word. When a building manager says
"nobody came this month", the firm has a record it wrote itself and nothing
the building agreed to. On paper this was the signed service form a
technician left in the machine room or got the caretaker to sign at the door.

This milestone puts that signature on the phone. At the end of a visit, the
person from the building signs on the technician's screen. The visit carries
the signature, who gave it and when, and every reader of the visit sees it:
the office, the portal and, later, the monthly customer report.

## Goals

- At the end of a completed visit, the technician either gets a signature
  from someone in the building or records why there isn't one. A missing
  signature is a fact with a reason, never silence.
- It works with no signal, like the rest of the visit: the signature waits
  in the outbox with the visit and goes when the connection returns.
- The office, the portal and the elevator's visit history show the signature,
  the signer's name and role, and the time.
- A visit edited after it was signed says so, everywhere the signature is
  shown.

## Non-goals

- **A legal electronic signature** (5070 sayılı Elektronik İmza Kanunu,
  qualified e-signature or mobile signature). This is an acknowledgement, like
  the paper form it replaces. It is not a contract signature.
- Verifying who signed: no SMS code, no ID number, no account for the signer.
- More than one signer per visit.
- Signatures on faults or inspections. Faults are the natural next place, but
  not in this milestone.
- The monthly customer report. It is the next milestone and will read what
  this one stores.

## Anti-goals (it fails if…)

- …a technician can't finish a visit because nobody in the building would
  sign. "Nobody there" and "declined" are valid answers.
- …a signature visit sent offline arrives without its image, or arrives
  twice.
- …a signature can be moved to another visit, or replaced without trace.
- …the signer's personal data goes beyond what the paper form held: a name
  and a role.

## The record

### On `MaintenanceVisit` (migration)

| Field | Meaning |
|---|---|
| `signature_status` | `signed`, `not_present` (nobody from the building was there) or `declined` (someone was there and would not sign). Blank on visits recorded before this milestone. |
| `signer_name` | Required when `signed`, otherwise empty. Up to 100 characters. |
| `signer_role` | `manager`, `caretaker`, `staff` or `other`, when `signed`. In Turkish: Yönetici, Kapıcı, Site/bina görevlisi, Diğer. |
| `signed_at` | When the person signed, by the device's clock, sent by the client. When it is missing or in the future, the server's time is used. |
| `amended_after_signature` | Set by the server when an owner or admin edits a signed visit. It is never cleared. |

### The image

- It is an `Attachment` with a new category `signature` and
  `object_type=maintenance_visit`, uploaded through the existing upload-url →
  PUT → confirm flow, after the visit is created, the same way photos are.
- It is a PNG of at most 200 KB.
- **One per visit.** A second confirm for the same visit is refused with
  `409 SIGNATURE_EXISTS`. The upload-url step also refuses a signature for a
  visit whose `signature_status` is not `signed`, with
  `409 SIGNATURE_NOT_EXPECTED`.
- A signature image can't be deleted through the attachments API, by any
  role. A wrongly attached one is fixed by an owner or admin editing the
  visit, which sets `amended_after_signature`.
- The read side carries `has_signature` and, for those who may see it, a
  signed URL to the image, under the same rules as the visit's photos.

## Rules

- **`completed` visits** require `signature_status`: `400`, field
  `signature_status`, code `required`. `incomplete` and `access_denied` visits
  may leave it blank. When there is somebody to sign for a cut-short visit,
  the technician can still record one.
- **`signed`** requires `signer_name` and `signer_role`. `not_present` and
  `declined` must leave them empty. Both are field errors.
- The image arrives after the visit. A `signed` visit without an image yet is
  shown as "imza bekleniyor" ("signature pending") to the office. If the image
  has not arrived after 24 hours, nothing chases it, but the visit list can be
  filtered by `signature=pending` (status `signed`, no image).
- **Edits after signing.** Today only an owner or admin can edit a visit
  (`maintenance_visit_edit`). That stays. Their edit to a signed visit
  succeeds and sets `amended_after_signature`. Its signature fields can't be
  edited: `signature_status`, `signer_name`, `signer_role` and `signed_at` are
  read-only after create.
- **Filters on `GET /maintenance-visits/`:** `signature=signed|pending|not_present|declined|none`
  (`none` is a blank status, from before this milestone).

## Web

- **The visit form (technician, phone).** After the checklist and notes, a
  **"Bina yetkilisi onayı"** step (building representative's approval) has
  three choices, none preselected:
  - **İmza al** (take a signature) opens the signature pad: a white drawing
    area at full width, about 240 px tall, with **Temizle** (clear), a name
    field and the role choice. Submit stays disabled until there is ink on the
    pad and a name.
  - **Binada kimse yoktu** (nobody was in the building).
  - **İmzalamayı reddetti** (declined to sign).
- **The pad.** Pointer events on a `<canvas>`, with no library. It works with
  a finger, a stylus and a mouse. It is drawn black on white in both themes,
  because it is a document. It exports with `canvas.toBlob("image/png")`,
  scaled to at most 600×240. An empty pad counts as no signature, whatever
  was tapped.
- **Offline.** The outbox stores the signature Blob beside the photos and the
  signer fields in the visit body. It is sent the same way: the visit, then
  the images, each idempotent.
- **Reading a visit.** The office visit detail shows the image, the name, the
  role and the time, or the reason when there is none. It shows "İmzadan sonra
  düzenlendi" (edited after signing) when amended, and "İmza bekleniyor"
  (signature pending) when the image hasn't arrived yet. The elevator's visit
  history and the visit list get a small chip: İmzalı, Kimse yoktu,
  Reddetti, or İmza bekleniyor. The list also gets the filter.
- **Portal.** The customer's visit view shows the same signature block, so the
  building can see who signed for it.

## Privacy

- The signer's name and role are personal data of someone who isn't a user,
  so the screen says what it is for, in one line under the pad: "İmza ve ad,
  bu bakımın yapıldığını onaylamak için saklanır." (The signature and name are
  kept to confirm this visit took place.)
- Retention follows the visit's. Nothing else about the signer is asked.

## Demo

Seeded completed visits get a signature status: mostly `signed`, with a
generated signature image (a few procedurally drawn strokes, never a real
person's signature) and a demo name. A few are `not_present`, and one is
`declined`. Refresh fills only visits with a blank status, per subject
(SL-161).

## Delivery

1. **API.** The migration, rules and filter; the attachment category and its
   one-per-visit and no-delete rules; the read fields; the demo data; new
   error codes `SIGNATURE_EXISTS` and `SIGNATURE_NOT_EXPECTED`. OpenAPI is
   updated.
2. **Web.** The signature pad component and the visit form step; the outbox
   carrying the Blob; the office detail, list chips and filter; the portal
   block; the error code translations.

## Acceptance

- [ ] A completed visit can't be submitted without a signature or a reason.
      An incomplete visit can.
- [ ] A signed visit submitted offline arrives once, with its image, when the
      connection returns.
- [ ] A second signature image for a visit is refused, and no role can delete
      one.
- [ ] An owner's edit to a signed visit marks it "edited after signing". The
      signature fields stay as they were.
- [ ] The office detail, the visit list chip, the elevator's history and the
      portal all show the same signature state.
- [ ] The demo tenant has signed, not-present and declined visits.
- [ ] Backend `make lint`, `make mypy`, `make check` and `make test` pass. Web
      `npm run verify` passes, and smoke renders the visit form step and the
      signed detail.

## Calls made without the user

- An acknowledgement, not a legal e-signature.
- Required for completed visits, with "nobody there" and "declined" as valid
  answers. Optional otherwise.
- Name and role only. No phone number, ID or code.
- One signer per visit.
- A PNG image, uploaded after the visit like photos, one per visit. It can't
  be deleted.
- Edits after signing stay possible for an owner or admin, but are marked and
  never cleared.
- The device's clock for `signed_at`, falling back to the server's when the
  device clock is missing or in the future.
- A canvas pad with no library, black on white in both themes.

## Left to the builder

- How the pad smooths strokes, and how much ink counts as "not empty".
- Whether `has_signature` is annotated on the list or read through the
  attachments prefetch the visit list already does.
- The demo signature drawing.
