# M6 — Customer portal

Date: 2026-10-21. Roadmap: M6 (`2026-10-20-phase-2-roadmap.md`, #91).
Builds on M1 (visits), M4 (faults) and M5 (inspections).

## Why

A firm's customer is a building manager or a site management company. Today
they learn what happened to their lifts by phoning the firm: "was the monthly
maintenance done?", "what did the inspector find?", "is someone on the way?".
Every one of those calls is an answer the product already holds.

The portal lets a customer's contact read those answers, and report a fault,
without a call and without seeing anything that belongs to another customer.

## Who

A **customer contact** (`CustomerContact`, Phase 1) whom the firm has invited.
It is not a staff user:
- they have no role in the permission matrix,
- they never see the staff API,
- a staff token never opens the portal.

## The principal — `PortalAccount` (`apps.portal`)

| Field | Meaning |
|---|---|
| `company`, `customer`, `contact` | The firm, the customer and the contact this account belongs to. One account per contact. |
| `email` | Unique among active portal accounts. Lowercased like `User.email`. |
| `password` | Hashed with the same hasher as staff. |
| `is_active` | Turned off by the firm, or by the contact being deleted. |
| `last_login_at` | |

### Why it is a separate model, not a `User` with a "customer" role

The staff API is protected by a role matrix written for five staff roles, and by
the technician narrowing. A sixth role would have to be denied **everywhere**,
in every future endpoint, and the one place someone forgets is a leak of one
customer's data to another. A separate principal is denied by construction:
- staff views authenticate `User` tokens only,
- portal views authenticate `PortalAccount` tokens only,
- neither class accepts the other's token.

### Tokens

- JWTs signed with the same key, with a distinct `aud` (`"portal"` against the
  staff tokens' default) and a `portal_account_id` claim instead of `user_id`.
- `CompanyAwareJWTAuthentication` rejects any token carrying
  `aud="portal"`. `PortalJWTAuthentication` accepts only those.
- The refresh token sits in its own httpOnly cookie (`portal_refresh`),
  scoped to `/api/v1/portal/`, and is rotated on use like staff refresh
  tokens. Its own session table works the way `RefreshSession` does.
- **Scope.** Every portal request runs in the account's firm context **and**
  is narrowed to its customer. There is a `PortalViewSet` base: the portal's
  equivalent of `TenantViewSet` plus the technician narrowing, written once.

### Invitation and login

| | |
|---|---|
| `POST /customers/{id}/portal-invitations/` | Office roles. Takes `{contact}`. The contact must belong to that customer and have an e-mail. The contact is e-mailed a single-use link to `{FRONTEND_URL}/portal/accept/<token>`, using the existing `OneTimeToken`. |
| `GET /customers/{id}/portal-accounts/`, `POST /portal-accounts/{id}/deactivate/` | Office roles. |
| `POST /portal/auth/accept`, `login`, `refresh`, `logout`, `password-reset`, `password-reset/confirm` | Anonymous. Throttled like the staff auth endpoints, with the same lockout rules (per e-mail and address). |
| `GET /portal/me` | The account, the contact's name, the customer's name, and the firm's name and phone, so the portal can show who to call. |

## What a contact sees (read-only, their customer only)

| | |
|---|---|
| `GET /portal/elevators/`, `/{id}/` | Name, building, status, the last and next maintenance dates, the inspection label and next inspection date. **Not** the technical fields, contract prices or internal notes. |
| `GET /portal/visits/`, `/{id}/` | Date, status, the checklist with results, the technician's first name, and photos as download URLs. **Not** work notes meant for the office (`work_notes` is internal). `parts_replaced` is shown. |
| `GET /portal/inspections/`, `/{id}/` | Label, date, body, report number, non-conformities with status, and the report PDF as a download URL. |
| `GET /portal/faults/`, `/{id}/` | The contact's own and the customer's faults: reference, status, the timeline (times only, no staff names except the technician's first name), and resolution notes. **Not** billable. |
| `POST /portal/faults/` | Reports a fault on one of the customer's lifts, with `channel=customer_portal`. It carries the same fields and priority mapping as the public QR report, without the honeypot or the merge, because the contact is identified. It raises the same notifications (`fault_reported` or `fault_emergency`). |

Downloads go through the existing signed-URL flow, checked against the
customer scope.

## Web (separate PR)

A separate route tree, `/portal/*`, with its own minimal layout, its own login
and its own session. The staff shell's session must never be mixed with it,
and a staff user who opens `/portal` sees the portal login.

| Screen | Contents |
|---|---|
| Login, accept invitation, password reset | |
| Home | The customer's lifts with status, the next maintenance and inspection dates, and any open faults. |
| Lift | Visit history, with checklist and photos; inspection history, with the report PDF. |
| Faults | The list, and "Arıza bildir" (with "Kabinde mahsur kalan var" and a prominent 112 note). |
| Header | Who to call: the firm's name and phone. |

The office gains a "Portal erişimi" section on the customer detail: invite a
contact, see the accounts, deactivate one.

## Non-goals

- Contacts editing anything other than their password.
- Billing and invoices in the portal (M7 can add it later).
- Messaging between the customer and the firm.
- Multiple customers per contact.

## Decisions made without the user

- A separate `PortalAccount` principal with its own token audience and cookie,
  not a staff role.
- Hidden from contacts: internal fields (`work_notes`, billable, prices,
  technical data).
- Shown: the technician's first name only.
- A portal fault report reuses the public QR priority mapping, without the
  honeypot or the merge.

## Slices

1. **Backend:** `apps.portal`, the principal, auth, invitation, the read API,
   fault reporting, and tests (including that tokens never cross between staff
   and portal, and a customer-scope isolation suite).
2. **Web:** the portal route tree and the office's "Portal erişimi" section.
