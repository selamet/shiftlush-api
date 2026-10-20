# M5 — Periodic inspection

Date: 2026-10-20. Roadmap: M5 (`2026-10-20-phase-2-roadmap.md`, #90).
Builds on M2 (notifications) and M4 (faults, which a non-conformity can open).

## Why

An elevator's year turns on its periodic inspection by an accredited A-type
inspection body. The label it leaves in the car decides whether the lift may
run, and a missed deadline gets it **sealed** by the municipality. Phase 1 kept
five inspection fields on the elevator with no module behind them. The firm
cannot record what the inspector found, cannot see what is due, and gets no
warning before a seal.

## The rules, from the regulation

**Source:** Asansör Periyodik Kontrol Yönetmeliği, RG 04.05.2018 No. 30411, as
amended by RG 13.09.2022 No. 31952. Read from the consolidated text on
2026-10-20. The roadmap required these to be confirmed, not assumed. The
product encodes them as data (below), so a later amendment is a change to
data, not to code.

| Label | Meaning | Correction deadline | If missed |
|---|---|---|---|
| `green` | No defects (*kusursuz*) | none | — |
| `blue` | Slightly defective (*hafif kusurlu*) | by the **next periodic inspection** | — |
| `yellow` | Defective (*kusurlu*) | **120 days**, then a follow-up check within 3 working days | **sealed** by the municipality |
| `red` | Unsafe (*güvensiz*); **the lift must not be used** | **60 days**, then a follow-up check within 3 working days | **sealed** |

Further rules from the same text:

- **The inspection itself** is at least once a year (m.8/1).
- **A red lift that a follow-up turns yellow** must reach blue or green
  within 120 days of the **original inspection date**, or it is sealed.
- **Blue-label follow-up.** The first follow-up for a blue label is free if
  the building applies within 60 days of the report's approval. The product
  can remind the firm of that window.
- **Extensions.** Neither the inspection body nor the municipality may extend
  a deadline. Only the Ministry may, in force majeure (m.9/18).

## The model — `apps.inspections`

### `Inspection`

| Field | Meaning |
|---|---|
| `company`, `elevator` | Tenant-scoped. |
| `kind` | `periodic` or `follow_up`. A follow-up links to the periodic inspection it follows (`follows`). |
| `inspected_on` | Date. |
| `inspection_body` | Text. The five Phase 1 fields keep their names. |
| `report_number` | Text. |
| `label` | `green`, `blue`, `yellow` or `red`. |
| `report` | Nullable FK to an `inspection_report` attachment (the PDF). |
| `next_inspection_due` | Defaults to `inspected_on` + 1 year. Editable, because the body sets the date. |
| `correction_deadline` | Derived from the label and the rules above. For a follow-up that turns red into yellow, it counts 120 days from the original periodic inspection. |

### `NonConformity`

| Field | Meaning |
|---|---|
| `inspection` | The inspection that found it. |
| `description` | What the inspector wrote. |
| `severity` | The label colour it caused. |
| `status` | `open`, `fixed` or `waived`. |
| `fault` | Nullable FK to the M4 fault opened to fix it. |
| `fixed_on` | Date. |

- **The latest inspection drives the elevator.** Recording one updates the
  elevator's `last_inspection_date`, `inspection_label`,
  `next_inspection_date`, `inspection_body` and `inspection_report_number`.
  Those fields become read-only on the elevator form, with a link to
  "record an inspection" instead.
- **A red label** sets an `active` lift to `out_of_service`, because the
  regulation forbids its use. A later green, blue or yellow result restores it.
  A seal is recorded as the elevator's existing `sealed` status, set by the
  office.
- **Opening a fault.** A non-conformity can open an M4 fault in one action:
  "arıza aç", which creates a fault on the elevator with the non-conformity's
  description, priority `high` for red and `normal` otherwise, and links the
  two.

### The deadline rules as data

`apps/inspections/rules.py` holds a table:
label → (lift usable?, deadline in days or "next inspection", counted from).
Each row cites its paragraph. The tests read the same table, so a rule change
is one edit, made in one place, together with its citation.

## Jobs and notifications (daily, M2 kinds)

| Kind | When | Who | E-mail default |
|---|---|---|---|
| `inspection_due` | 30 days before `next_inspection_date` | owner, admin, operations | on |
| `correction_deadline_near` | 30, 14, then 3 days before a yellow or red deadline, while non-conformities are open | the same, plus the technicians assigned to the customer | on |
| `correction_deadline_passed` | the day after, while non-conformities are open. **Risk of sealing.** | the same | **always e-mailed** |
| `inspection_red_label` | when a red label is recorded | the same | **always e-mailed** |

Each notification is deduped per (inspection, kind, threshold).

## API

| | |
|---|---|
| `GET` / `POST /inspections/`, `GET` / `PATCH /inspections/{id}/` | Filters: `elevator`, `label`, `due_before`, `has_open_nonconformities`. The read side carries `correction_deadline` and `days_left`. Write: owner, admin, operations. Read: those, plus technicians (narrowed). |
| `POST /inspections/{id}/nonconformities/`, `PATCH /nonconformities/{id}/` | Status, `fixed_on`. |
| `POST /nonconformities/{id}/open-fault/` | Creates the M4 fault and links it. |
| `GET /inspections/due/` | The firm-wide list of what is due in a window: inspections, and correction deadlines. |

## Web (separate PR)

- **"Muayeneler" (`/inspections`):** a firm-wide view of what is due:
  upcoming inspections, and open correction deadlines with days left and the
  sealing risk. Red and yellow are distinct from system status colours, as
  spec §20 requires, and never shown by colour alone.
- **Elevator detail:** the inspection history, with each label, report PDF,
  non-conformities and their faults, and a "Muayene kaydı gir" action.
- **The inspection form:** body, report number, date, label, an optional PDF,
  and the list of non-conformities.
- Turkish strings for the new notification kinds.

## Non-goals

- Booking the inspection with the body, or integrating with the Ministry's or
  a body's systems.
- Fees (*ücret*).
- Elevator identity labels (EK-9).

## Decisions made without the user

- The deadline table above, encoded as data with citations.
- A red label takes the lift out of service automatically, and a seal stays a
  manual status.
- Notification thresholds of 30, 14 and 3 days. The two sealing-risk kinds
  ignore e-mail preferences.
- A non-conformity's fault is high priority for red and normal otherwise.

## Slices

1. **Backend:** the models, rules, elevator sync, non-conformity → fault, the
   API, and the jobs and notifications.
2. **Web:** "Muayeneler", the history on the elevator, and the inspection form.
