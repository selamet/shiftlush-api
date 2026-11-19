# The customer address book

Date: 2026-11-19. Issue #166. Spans `shiftlush-api` and `shiftlush-web`.

Decisions the user made are marked **(user)**. Everything else is a default
chosen while writing this and is open to correction.

## Why

A firm user reported that they type the same address again and again: once on
the customer, once on the site, and once more on every building of that site.
Today the three records each carry their own address columns
(`neighborhood`, `street`, `building_number`, ...), the three forms each
render their own `AddressSelect`, and nothing carries an address from one
record to the next. The neighbourhood picker alone is three chained selects
with a remote search, so a customer with one site and four blocks means
walking that chain six times for what is, to the user, one address.

## Goals

- **A customer has a list of saved addresses** that the firm can add to, edit
  and remove. **(user)**
- **Addresses live in their own table**, linked to the customer. **(user)**
- **One of them can be the billing address.** **(user)** The address a
  customer has today becomes it.
- **The site and building forms offer "pick from saved addresses"** and fill
  the form from the chosen one. **(user)**
- Picking an address fills everything the form can hold: the neighbourhood
  chain, street, building number, the directions note and the map pin.

## Non-goals

- **Linking a building or site to an address row.** A pick copies the values;
  the building keeps its own columns. **(user: approach A)** Reports, QR
  labels, route planning and the visit filters go on reading the building's
  address and are not touched.
- **Printing the billing address on documents.** No invoice, statement or
  report prints a customer address today. The billing address is stored and
  shown on the customer page; putting it on a document is separate work.
- **Propagating a correction.** Fixing a saved address does not change the
  buildings that were filled from it earlier.
- **A firm-wide address book.** Addresses belong to one customer. A building
  form lists only the selected customer's addresses.
- **The customer portal.** Portal users neither see nor edit the list.
- **The firm's own address** (`Company`) stays as it is.

## Anti-goals

- **An address that fills itself in.** `AddressSelect` deliberately never
  writes a value the user did not accept, because a wrong address ends up on
  a QR label and a crew is sent to it. Picking a saved address is an explicit
  press, the result stays editable, and nothing is pre-selected when the form
  opens.
- **Two places to keep a customer's address.** After this change the list is
  the only store. A customer row with its own address columns next to a
  separate list would bring the duplicate typing back in a new form.

## Approaches

**A. An address book whose entries are copied on pick — chosen (user).**
A `CustomerAddress` table. The site and building forms copy the picked entry
into their own fields. Everything that reads a building's address is
untouched. The cost: correcting a saved address later does not reach the
buildings filled from it.

**B. An address book that buildings link to.** Buildings and sites hold a
foreign key instead of columns, so one correction reaches all of them.
Rejected: one edit silently moves several buildings, blocks of one site still
differ by building number and so need their own field anyway, and every
reader of a building's address (reports, routing, labels, filters) would have
to be rewritten.

**C. No table; derive the list from existing records.** The picker lists the
addresses already on the customer and its sites and buildings, and the
billing address becomes a second set of columns on the customer. Smallest,
but the user cannot add an address on its own or name one, which is what was
asked for.

## The model

`CustomerAddress` in `apps/customers`, a `CompanyOwnedModel` like
`CustomerContact`, table `customer_address`.

| Field | Notes |
|-------|-------|
| `customer` | FK, `PROTECT`, `related_name="addresses"` |
| `label` | Short name the user gives it ("Head office", "Site entrance"). Required, max 60. |
| `neighborhood` | FK to `address.Neighborhood`, nullable, `PROTECT` |
| `street`, `building_number`, `unit_number` | As on `Customer` today |
| `address_note` | Free-text directions, as on `Building` |
| `latitude`, `longitude` | Optional pin, as on `Building` |
| `is_billing` | The billing address |

Rules:

- At most one live billing address per customer, enforced with a partial
  unique constraint, the way `uq_customer_primary_contact` does it.
- Saving an address with `is_billing` set moves the flag: the row that held
  it loses it in the same transaction, the way `demote_other_primaries` does
  for contacts. The client never has to clear the old one first.
- Nothing is promoted automatically. A customer may have no billing address.
- An address must hold something: at least one of neighbourhood, street,
  building number, unit number or address note. A label alone is rejected.
- Latitude and longitude are not validated as a pair, because `Building` and
  `Complex` do not validate them either and `position_of` already treats half
  a position as none.

There is no separate "main address" flag. The address the customer carries
today is already presented as the billing address (see below), and nothing
asked for a second kind.

## What happens to the customer's own address

The web app already calls the customer's address the billing address: the
form's fieldset is `customer.billingAddress` and the detail page prints it
under `customerDetail.billingAddress`. So the existing address becomes the
customer's billing address in the book, and the columns `neighborhood`,
`street`, `building_number` and `unit_number` leave the `customer` table.

- A data migration creates one `CustomerAddress` per customer that has any of
  the four filled, with `is_billing=True` and the label "Fatura adresi".
  Customers with an empty address get no row. The migration is reversible:
  backwards, it copies the billing address onto the customer.
- The flat address fields leave **both** the customer read and the customer
  write. Nothing would read them after this change: the detail page turns
  into the addresses card, the edit form loses its address block, and no
  list, report, portal view or label prints a customer address.
- The customer read gains `addresses`, the full list, prefetched so the list
  endpoint stays at a fixed query count.
- The customer write does not accept addresses. This codebase has no nested
  write (contacts are created with a second request), and this is not the
  feature to introduce one.

This is a breaking change to the customer contract and goes in
`CHANGELOG-API.md`. The web app is the only client. API and web are deployed
together; in the minutes between the two, the old web form cannot save a
customer and the old detail page shows no address. Nothing is lost.

## The API

Mirrors the contacts endpoints.

- `GET, POST /customers/{id}/addresses` — the addresses of one customer.
- `GET, POST /customer-addresses`, `GET, PATCH, DELETE
  /customer-addresses/{id}` — the flat resource, filterable by `customer` and
  `is_billing`.
- Read fields: the model's fields plus the named address through
  `AddressReadMixin`.
- **`AddressReadMixin` gains `district_id` and `province_id`.** It serves
  names only today, and a picker cannot fill the province and district
  selects from names. The addition is non-breaking and reaches every read
  that uses the mixin (customer address, site, building), which is what the
  "Site address" entry below needs.
- Ordering: billing first, then label.
- Permissions, tenant filtering and the technician narrowing are whatever
  contacts use. Delete is a soft delete.
- Changes are audited like any other `CompanyOwnedModel`.

Demo data (`core/demo.py` and the seed commands) creates the customer's
address as a billing `CustomerAddress`, reads it from there when it derives
site and building addresses, and gives a few demo customers a second,
non-billing address so the picker has something to show in a demo account.

## The web app

**Customer page.** The billing address line becomes an "Addresses" card: one
row per address with its label, the address, and a "Billing" badge on the one
that carries it. Add, edit and delete from the card. The address form is the
existing `AddressSelect` with the pin, plus label, street, numbers, note and
the billing tick.

**Customer form.** On create, the "Billing address" block stays exactly as it
is today: no label input, no note, no pin. After the customer is created, the
form sends a second request that saves the block as an address with
`is_billing=true` and the label "Fatura adresi", and only when something was
typed in it. If that second request fails, the customer exists, the user
lands on its page and is told the address was not saved. On edit, the block
is gone; addresses are managed from the card.

**Site and building forms.** Above the address fields, a "Pick from saved
addresses" select. It is disabled until a customer is chosen and hidden when
there is nothing to offer. It lists the customer's addresses by label and
address. In the building form, when a site is selected and has an address,
the site's own address is listed first as "Site address", because blocks of
one site share it and it is usually not in the book.

Picking an entry fills province, district and neighbourhood, street, building
number, address note (building only) and the pin. `AddressSelect` is extended
to accept a complete starting value (the three ids and names) so that all
three selects show the picked address and stay editable; today it can only be
started from a neighbourhood id, which leaves the selects above it empty and
the neighbourhood locked.

Two limits on the pin:

- It is copied only when the source has both coordinates.
- "Site address" never copies the pin. A building without a pin already falls
  back to its site's position, and leaving it empty keeps that fallback
  following later corrections to the site.

Every field stays editable, and picking again replaces what the previous pick
wrote. Changing the customer clears the select but not what was already
filled in. Nothing is saved to the book from these forms.

```
Customer   [ Yildiz Sitesi Yonetimi        v ]
Site       [ Yildiz Sitesi                 v ]

Address
  Pick from saved addresses
  [ Site address - Cumhuriyet Mah., Ataturk Cd. 12   v ]

  Province [ Istanbul v ]  District [ Kadikoy v ]
  Neighbourhood [ Cumhuriyet v ]
  Street [ Ataturk Cd. ]   Building no [ 12/A ]
```

Turkish and English strings go in the message files.

## Constraints

- SQLite locally, PostgreSQL in production: the partial unique constraint
  and the data migration must run on both.
- The generated OpenAPI contract (`openapi/v1.yaml`) and the web's generated
  client are regenerated, not hand-edited.
- The existing suites that assert tenant isolation, query counts, field error
  codes and the API contract are extended to the new endpoints.
- Work follows issue, branch, pull request in each repo; the web repo gets
  its own issue.

## Left to the builder

- How `AddressSelect` takes a complete starting value, and how a pick resets
  it (it is uncontrolled today).
- The error code for the "address must hold something" rule, following
  `core/error_codes.py`.
- Order of work: the API first, then the web against the regenerated client.
