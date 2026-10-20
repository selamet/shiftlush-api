# M1 — The maintenance visit on the phone

Date: 2026-10-20. Roadmap: M1 (`2026-10-20-phase-2-roadmap.md`, #86).
Backend record: SL-119 (#83), plus SL-124 (#95) for who a visit may name.

## Why

A technician in a machine room has to record the monthly visit, often on a weak
connection. The record already exists in the API; nothing in the web app can
create one. Every month a firm cannot record visits in the product is a month
its technicians keep paper forms, and the product's
`last/next_maintenance_date` stays empty.

## Who and where

- **Technician, on a phone.** The flow starts from the elevator's QR label
  (`/scan` → `/q/$token` → elevator) and records one visit.
  - One-handed, big targets, readable in a dark machine room: spec §20, and
    the design brief's mobile rules.
  - The connection can drop at any point, so submitting must survive that.
- **Office (owner, admin, operations), on a desktop.**
  - Sees each elevator's visit history and its last and next maintenance
    dates.
  - Browses all visits.
  - Can record a visit on a technician's behalf.

## What gets built

### Backend (one PR)

1. **`Idempotency-Key` becomes mandatory on `POST /maintenance-visits/`.**
   - Spec §8.10 called for this in Phase 2. It is the endpoint most likely
     to be retried on a bad connection.
   - A missing key is `400` with a new code, `IDEMPOTENCY_KEY_REQUIRED`.
2. **`items` is accepted inline on create.**
   - It is optional, uses the same shape as `set-checklist`, and is applied
     in the same transaction as the visit.
   - One request then records the whole form. A visit can no longer exist on
     the server without its checklist because the second call never arrived,
     which is the failure the offline outbox would otherwise produce.
   - `set-checklist` stays, for corrections by the office.
3. **Create is atomic.** The visit, its items and the elevator's
   recalculated dates commit together or not at all. Today they are three
   separate writes.
4. **Elevator visit history.** `GET /maintenance-visits/?elevator=<id>`
   already exists (`MaintenanceVisitFilter`). Nothing to add.

### Frontend: the visit flow (one PR)

- **Entry points:**
  - A **"Bakım kaydı gir"** primary action on `ElevatorDetailMobile`, for a
    technician assigned to the elevator's customer and for the office.
  - The same action on the desktop elevator detail.
  - Both are hidden when the elevator has no `current_contract`. The reason is
    shown instead: *"Aktif sözleşme yok — bakım kaydı girilemez."*
- **`/elevators/$id/visits/new`**, mobile-first, one column:
  - **Status:** `completed` (the default), `incomplete`, or `access_denied`.
  - **Checklist:** 16 rows, each a three-way choice: Tamam / Dikkat gerekli /
    Uygulanmaz.
    - No choice is preselected, because a preselected "Tamam" is a signature
      nobody gave.
    - A counter shows answered rows ("12/16").
    - When the status is `completed`, every row must be answered before
      submit is enabled.
    - A note field opens under a row marked "Dikkat gerekli".
  - **For `incomplete` and `access_denied`:** the checklist is optional and
    `status_note` is required, which is the only way to say why.
  - **Free text:** `work_notes` and `parts_replaced`.
  - **Photos:** optional, up to 4. The `<input>` gets `accept="image/*"` and
    `capture="environment"`. Photos upload after the visit is created, through
    the existing upload-url → PUT → confirm flow, with
    `object_type=maintenance_visit`.
  - **Implicit fields:** `visit_date` is today, set when the form opens.
    `contract` comes from the elevator's `current_contract`. `technician` is
    omitted for a technician; the office picks one.
- **`/maintenance-visits`** (office): a list with filters for status,
  technician and date, using the existing list components.
- **`/maintenance-visits/$id`**: detail showing the checklist with results,
  the notes and the photos.
- **Elevator detail:** last and next maintenance dates, and a "Bakım
  geçmişi" panel with the latest visits.
- **Strings and codes:** Turkish strings and enum labels for the checklist
  items, results and statuses. Turkish messages for
  `TECHNICIAN_RECORDS_OWN_VISITS` and `IDEMPOTENCY_KEY_REQUIRED`.

### Frontend: offline (one PR)

- **PWA shell.** A web app manifest, installable, and a service worker that
  precaches the built app shell. API responses are **not** cached: the
  product shows live data or says it cannot.
- **Outbox.**
  - Submitting a visit writes it to an IndexedDB outbox first, with its
    idempotency key, its body and its photos as Blobs, and only then sends it.
  - A successful send removes the entry.
  - The same key is reused on every retry, so a retry can never record the
    visit twice: the server replays the stored response.
- **Retries.**
  - Retried on the `online` event, on app start once the session has been
    restored, and every 30 s while entries are waiting.
  - Network errors and 5xx back off.
  - A 4xx stops retrying and marks the entry **"needs attention"**, showing
    the server's message, with *Discard* as the only action. A rejected form
    is not silently retried forever.
- **What the technician sees.** "Gönderilmeyi bekliyor (N)" on the shell
  while entries wait, and a list of them with their state. The success screen
  after submit says either *"Kaydedildi"* or *"Bağlantı yok — bağlantı gelince
  gönderilecek"*, never a spinner that waits on the network.
- **Session.** The access token lives in memory, so after a reload the
  outbox waits for the refresh. A device whose session has expired keeps its
  entries until somebody signs in again **as the same user**. Entries are
  keyed by user id and never sent under another login.

## Non-goals

- Editing a submitted visit as a technician. The API refuses it.
- Caching API reads for offline browsing.
- Push notifications.
- Starting a visit offline from a QR scan when the elevator's detail has never
  been loaded. The flow needs the elevator and its `current_contract` once,
  which the scan's own request fetches.

## Decisions made without the user (sizes and defaults)

- No preselected checklist answers, and every row required when `completed`.
- Up to 4 photos per visit.
- The PWA uses `vite-plugin-pwa` with a version at least two weeks old. The
  outbox uses raw IndexedDB behind a small module, with no extra library.
- **Order:** backend → visit flow → offline. Each ships on its own, and the
  visit flow is useful online before the outbox exists.

## Acceptance criteria

- [ ] Backend: key required on visit create, inline `items`, atomic create,
      with tests, contract and changelog updated
- [ ] A technician can scan → open the elevator → record a completed visit
      with 16 answers and a photo, on a phone-width screen
- [ ] The office sees the visit in the elevator's history and in
      `/maintenance-visits`, with its checklist and photo
- [ ] A visit submitted offline is sent when the connection returns, exactly
      once
- [ ] A visit the server rejects is shown as needing attention, not retried
- [ ] `npm run verify` and the backend gates pass
