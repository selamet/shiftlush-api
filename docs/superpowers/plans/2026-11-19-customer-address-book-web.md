# Customer Address Book (Web) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show and manage a customer's saved addresses, and let the site and building forms fill their address from one.

**Architecture:** The customer read already arrives with `addresses`, so no new list query is needed: the customer page and the two forms read the list off the customer they already hold. Everything that decides something (what the picker offers, what a pick fills in, whether the create form's block is worth saving) is a pure function in `src/lib/address-book.ts`, tested with vitest; the screens only wire those functions to inputs. A pick refills uncontrolled inputs by remounting the address block with a new `key`.

**Tech Stack:** React 19, TanStack Router and Query, react-i18next (Turkish only, `messages/tr.json`), vitest, a client generated from `openapi/v1.yaml`.

**Spec:** `docs/superpowers/specs/2026-11-19-customer-address-book-design.md` in `shiftlush-api`.

**Depends on:** the API plan (`2026-11-19-customer-address-book-api.md`) merged to `shiftlush-api` main, so `npm run api:sync` can pull the contract.

## Global Constraints

- Repo `shiftlush-web`, issue #134, branch `GH-134`, in a fresh worktree `~/projects/shiftlush-web-GH-134` off `origin/main`. Never switch the HEAD of the main checkout.
- Commits: `GH-134-<type>: <English description>`. No AI attribution lines in commits or the PR.
- Code and comments are English. User-facing text is Turkish and lives only in `messages/tr.json`; `npm run lint:tr` fails on Turkish in source and `npm run lint:i18n` on a missing or unused key.
- `src/api/generated.ts` and `openapi/v1.yaml` are produced by `npm run api:sync` and `npm run api:generate`, never edited by hand.
- No native `<select>` (`npm run lint:selects`); use `SearchableSelect`.
- Nothing is pre-selected in the picker when a form opens, and a pick never happens without a press.
- `npm run verify` is the gate before the PR.

## Review Focus

1. **A pick over fields the user already typed in.** The pick wins and replaces them; this is the stated behaviour, and the fields must show the new values, not the stale typed ones. Test in Task 2 (`fillFrom`) and checked by hand in Task 6.
2. **A saved address with no neighbourhood.** The pick fills street and numbers and leaves the three selects empty and usable, not locked. Test in Task 3.
3. **Half a pin on the source** (latitude without longitude). No pin is copied. Test in Task 2.
4. **The create form's second request failing.** The customer exists; the user lands on its page and is told the address was not saved. No silent loss, no duplicate customer on retry. Checked in Task 5.
5. **Changing the customer after a pick.** The picker resets and offers the new customer's addresses; the filled fields stay. Checked in Task 6.

---

### Task 1: Contract and API functions

**Files:**
- Modify: `openapi/v1.yaml`, `src/api/generated.ts` (generated)
- Modify: `src/api/queries.ts`
- Test: `npm run typecheck`

**Interfaces:**
- Produces, in `src/api/queries.ts`:
  - `type CustomerAddress = Schemas["CustomerAddressRead"]`
  - `type CustomerAddressWrite = Schemas["CustomerAddressWriteRequest"]`
  - `createCustomerAddress(customerId: string, body: Omit<CustomerAddressWrite, "customer">, idempotencyKey?: string)` → `Promise<CustomerAddress>`, posting to `/customer-addresses/` with `customer` added, exactly as `createCustomerContact` does
  - `updateCustomerAddress(id: string, body: Partial<CustomerAddressWrite>)` → `PATCH /customer-addresses/{id}/`
  - `deleteCustomerAddress(id: string)` → `DELETE /customer-addresses/{id}/`

- [ ] **Step 1: Create the worktree.** `git -C ~/projects/shiftlush-web fetch origin && git -C ~/projects/shiftlush-web worktree add -b GH-134 ../shiftlush-web-GH-134 origin/main`, then `npm ci` inside it.

- [ ] **Step 2: Pull the contract.** `npm run api:sync && npm run api:generate`. Expected: `generated.ts` gains `CustomerAddressRead`, `CustomerRead` loses `street` and gains `addresses`.

- [ ] **Step 3: Run `npm run typecheck`.** Expected: FAIL in `CustomerFormScreen.tsx` and `CustomerDetailScreen.tsx`, which still read the removed fields. These failures are the work list for Tasks 4 and 5; leave them.

- [ ] **Step 4: Add the types and three functions** listed under Interfaces to `src/api/queries.ts`, next to the contact ones. Mutations that change an address invalidate the same customer queries the contact mutations invalidate.

- [ ] **Step 5: Commit** (typecheck is red until Task 5; say so in the message body).

```bash
git add openapi/v1.yaml src/api/generated.ts src/api/queries.ts
git commit -m "GH-134-core: sync the address book contract"
```

---

### Task 2: The address-book rules as pure functions

**Files:**
- Create: `src/lib/address-book.ts`
- Test: `tests/address-book.test.ts`

**Interfaces:**
- Consumes: `CustomerAddress`, `Complex` types from `src/api/queries.ts`; `Coordinates` and `parseCoordinates` from where `BuildingFormScreen.tsx` imports them.
- Produces:

```ts
export type FilledAddress = {
  provinceId: number | null; provinceName: string | null;
  districtId: number | null; districtName: string | null;
  neighborhoodId: number | null; neighborhoodName: string | null;
  street: string; buildingNumber: string; addressNote: string;
  pin: Coordinates | null;
};
export type PickOption = { value: string; kind: "site" | "saved"; label: string; fill: FilledAddress };

/** "Cumhuriyet Mah., Ataturk Cd. 12 / 3 · Kadikoy · Istanbul"; null when nothing is set. */
export function addressLine(a: AddressParts): string | null;
/** Site first (kind "site", value "site"), then saved addresses in the order given. */
export function pickOptions(addresses: CustomerAddress[], site: Complex | null): PickOption[];
/** True when the create form's block holds anything worth a second request. */
export function hasAddressContent(v: { neighborhood?: string; street?: string; building_number?: string; unit_number?: string }): boolean;
```

`AddressParts` is the structural subset of fields `addressLine` reads, so it accepts a `CustomerAddress`, a `Complex` and a `Building`.

- [ ] **Step 1: Write the failing tests** in `tests/address-book.test.ts` (plain objects cast to the types, as the other tests in `tests/` do).

```ts
describe("pickOptions", () => {
  it("offers nothing when there is no saved address and no site address", () => {
    expect(pickOptions([], null)).toEqual([]);
    expect(pickOptions([], siteWithoutAddress)).toEqual([]);
  });
  it("lists the site first, then the saved addresses in the order given", () => {
    expect(pickOptions([billing, depot], site).map((o) => o.kind)).toEqual(["site", "saved", "saved"]);
    expect(pickOptions([billing, depot], site)[0].value).toBe("site");
    expect(pickOptions([billing, depot], site)[1].value).toBe(billing.id);
  });
  it("carries ids and names for all three levels", () => {
    const { fill } = pickOptions([billing], null)[0];
    expect(fill).toMatchObject({ provinceId: 34, districtId: 449, neighborhoodId: 9001,
      provinceName: "Istanbul", districtName: "Kadikoy", neighborhoodName: "Cumhuriyet" });
  });
  it("copies the pin of a saved address only when both coordinates exist", () => {
    expect(pickOptions([withPin], null)[0].fill.pin).toEqual({ lat: 40.99, lng: 29.03 });
    expect(pickOptions([{ ...withPin, longitude: null }], null)[0].fill.pin).toBeNull();
  });
  it("never copies the site's pin", () => {
    expect(pickOptions([], siteWithPin)[0].fill.pin).toBeNull();
  });
  it("gives the site no address note and takes its building number", () => {
    expect(pickOptions([], site)[0].fill).toMatchObject({ addressNote: "", buildingNumber: "12" });
  });
});

describe("addressLine", () => {
  it("is null for an address with nothing set", () => { expect(addressLine(empty)).toBeNull(); });
  it("joins what is there and skips what is not", () => {
    expect(addressLine({ ...empty, street: "Ataturk Cd.", building_number: "12" })).toBe("Ataturk Cd. 12");
  });
});

describe("hasAddressContent", () => {
  it("is false for an untouched block", () => {
    expect(hasAddressContent({ neighborhood: "", street: "  ", building_number: "", unit_number: "" })).toBe(false);
  });
  it("is true for any one field", () => {
    expect(hasAddressContent({ unit_number: "5" })).toBe(true);
  });
});
```

Match `Coordinates`' real key names when writing the pin assertions.

- [ ] **Step 2: Run** `npx vitest run tests/address-book.test.ts`. Expected: FAIL, module not found.

- [ ] **Step 3: Implement the three functions.** `addressLine` replaces the local `billingAddress()` helper in `CustomerDetailScreen.tsx` (Task 4 deletes that helper); keep its output format. A site counts as having an address when `addressLine(site)` is not null.

- [ ] **Step 4: Run the test again.** Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/lib/address-book.ts tests/address-book.test.ts
git commit -m "GH-134-feat: address book rules for the picker"
```

---

### Task 3: `AddressSelect` opens on a complete address

**Files:**
- Modify: `src/components/forms/AddressSelect.tsx`
- Create: `src/lib/address-select-state.ts`
- Test: `tests/address-select-state.test.ts`

**Interfaces:**
- Produces: `AddressSelect`'s `initial` prop becomes

```ts
initial?: {
  provinceId?: number | null; provinceName: string | null;
  districtId?: number | null; districtName: string | null;
  neighborhoodId: number | null; neighborhoodName?: string | null;
};
```

  Existing callers that pass only the three old keys keep compiling and keep today's behaviour.
- Produces: `initialAddressState(initial)` in `src/lib/address-select-state.ts`, returning `{ provinceId, districtId, neighborhoodId, districtLabel, neighborhoodLabel }`, which the component's `useState` initialisers read.

- [ ] **Step 1: Write the failing tests.**

```ts
it("opens all three levels when the ids are given", () => {
  expect(initialAddressState({ provinceId: 34, provinceName: "Istanbul", districtId: 449,
    districtName: "Kadikoy", neighborhoodId: 9001, neighborhoodName: "Cumhuriyet" }))
    .toEqual({ provinceId: 34, districtId: 449, neighborhoodId: 9001,
      districtLabel: "Kadikoy", neighborhoodLabel: "Cumhuriyet" });
});
it("keeps the old label when only names are given", () => {
  expect(initialAddressState({ provinceName: "Istanbul", districtName: "Kadikoy", neighborhoodId: 9001 }))
    .toEqual({ provinceId: null, districtId: null, neighborhoodId: 9001,
      districtLabel: "", neighborhoodLabel: "Istanbul · Kadikoy" });
});
it("leaves everything empty and unlocked for an address with no neighbourhood", () => {
  expect(initialAddressState({ provinceName: null, districtName: null, neighborhoodId: null }))
    .toEqual({ provinceId: null, districtId: null, neighborhoodId: null, districtLabel: "", neighborhoodLabel: "" });
});
it("is the empty state when nothing is passed", () => {
  expect(initialAddressState(undefined).neighborhoodId).toBeNull();
});
```

- [ ] **Step 2: Run them.** Expected: FAIL, module not found.

- [ ] **Step 3: Implement `initialAddressState`** and have `AddressSelect` initialise `provinceId`, `districtId`, `neighborhoodId`, `districtLabel` and `neighborhoodLabel` from it. The province select takes its label from the loaded province list, as it does today. Update the three existing callers that edit a record (`BuildingFormScreen`, `ComplexFormScreen`, `SettingsScreen` if it uses it) to pass the new ids and `neighborhoodName` from the record, so editing a building also opens with all three selects filled.

- [ ] **Step 4: Run** `npx vitest run tests/address-select-state.test.ts`. Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/components/forms/AddressSelect.tsx src/lib/address-select-state.ts tests/address-select-state.test.ts src/screens
git commit -m "GH-134-feat: open the address picker on a complete address"
```

---

### Task 4: The addresses card and the address form

**Files:**
- Create: `src/screens/AddressFormScreen.tsx`
- Modify: `src/screens/CustomerDetailScreen.tsx`, `src/router.tsx`, `messages/tr.json`

**Interfaces:**
- Consumes: `createCustomerAddress`, `updateCustomerAddress`, `deleteCustomerAddress` (Task 1); `addressLine` (Task 2); `AddressSelect` with the full `initial` (Task 3).
- Produces: routes `/customers/$id/addresses/new` and `/customers/$id/addresses/$addressId`, both rendering `AddressFormScreen`, registered beside the two contact routes with the same customer loader.

- [ ] **Step 1: Add the strings** to `messages/tr.json`:

| Key | Text |
|-----|------|
| `customerDetail.addresses` | Adresler |
| `customerDetail.noAddresses` | Kayıtlı adres yok |
| `customerDetail.addAddress` | Adres ekle |
| `customerDetail.billingBadge` | Fatura |
| `addressForm.newTitle` | Yeni adres |
| `addressForm.editTitle` | Adresi düzenle |
| `addressForm.fields.label` | Adres adı |
| `addressForm.labelHint` | Örn. Merkez ofis, Şantiye girişi |
| `addressForm.fields.addressNote` | Adres tarifi |
| `addressForm.fields.isBilling` | Fatura adresi olarak kullan |
| `addressForm.billingHint` | Bir müşterinin tek fatura adresi olur; işaretlerseniz öncekinin işareti kalkar. |
| `addressForm.deleteConfirm` | Bu adres silinsin mi? Bu adresten doldurulmuş site ve binalar değişmez. |

Reuse `address.fields.*` for street, building number and unit number. Remove `customerDetail.billingAddress` and `customerDetail.noAddress` once nothing reads them.

- [ ] **Step 2: Build `AddressFormScreen`** as a sibling of `ContactFormScreen` (same route params, same form helper, same save/delete/redirect-to-customer flow, same confirm dialog component for delete). Fields in order: label (required, max 60), `AddressSelect` with `required={false}` and `location` (the pin), street, building number, unit number, address note (textarea), billing checkbox. Server field errors map to fields the way the contact form maps them; `ADDRESS_EMPTY` arrives on `label`.

- [ ] **Step 3: Replace the billing address line** in `CustomerDetailScreen.tsx` with an "Adresler" card built like the contacts card: an "Adres ekle" action linking to the new route, one row per `customer.addresses` entry showing the label, `addressLine(address)` and the "Fatura" badge when `is_billing`, each row linking to its edit route; the empty state when the list is empty. Delete the local `billingAddress()` helper.

- [ ] **Step 4: Run** `npm run lint:i18n && npm run lint:tr && npm run lint:buttons`. Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/screens/AddressFormScreen.tsx src/screens/CustomerDetailScreen.tsx src/router.tsx messages/tr.json
git commit -m "GH-134-feat: addresses card and address form on the customer"
```

---

### Task 5: The customer form

**Files:**
- Modify: `src/screens/CustomerFormScreen.tsx`, `messages/tr.json`

**Interfaces:**
- Consumes: `createCustomerAddress` (Task 1), `hasAddressContent` (Task 2).

- [ ] **Step 1: Add the string** `customer.addressNotSaved`: "Müşteri kaydedildi ancak fatura adresi kaydedilemedi. Adresi müşteri sayfasından ekleyebilirsiniz."

- [ ] **Step 2: Change the save.** The customer body no longer carries `neighborhood`, `street`, `building_number`, `unit_number` (lines 97-100 today). On **create**, after the customer is created and when `hasAddressContent(values)` is true, call `createCustomerAddress(created.id, { label: t("customer.billingAddress"), is_billing: true, neighborhood, street, building_number, unit_number })`. Whether that call succeeds or fails, navigate to the new customer's page; on failure show `customer.addressNotSaved` there using the app's existing notice mechanism for a message that survives a navigation (if there is none, pass it through router state and render an `Alert` at the top of the detail page). The form must not be left in a state where pressing save again creates a second customer.

- [ ] **Step 3: Remove the address fieldset on edit.** On create it stays exactly as it is; on edit it is not rendered, and the record's address is managed from the card.

- [ ] **Step 4: Run `npm run typecheck`.** Expected: PASS; the failures from Task 1 are gone.

- [ ] **Step 5: Check it by hand** against a local API (`make dev` in `shiftlush-api-GH-166` after `make migrate`): create a customer with an address and see one "Fatura" row on its page; create one with the block untouched and see the empty state; edit a customer and see no address block. Then stop the API between the two requests is not practical, so force the failure once by temporarily posting an empty label and confirm the notice appears and exactly one customer exists; revert the temporary change.

- [ ] **Step 6: Commit.**

```bash
git add src/screens/CustomerFormScreen.tsx src/screens/CustomerDetailScreen.tsx messages/tr.json
git commit -m "GH-134-feat: save the customer form's billing address to the address book"
```

---

### Task 6: The picker in the site and building forms

**Files:**
- Create: `src/components/forms/SavedAddressPicker.tsx`
- Modify: `src/screens/BuildingFormScreen.tsx`, `src/screens/ComplexFormScreen.tsx`, `messages/tr.json`

**Interfaces:**
- Consumes: `pickOptions`, `PickOption`, `FilledAddress` (Task 2); `AddressSelect` (Task 3).
- Produces: `SavedAddressPicker({ options, onPick }: { options: PickOption[]; onPick: (fill: FilledAddress) => void })`. Renders nothing when `options` is empty.

- [ ] **Step 1: Add the strings.**

| Key | Text |
|-----|------|
| `addressPicker.label` | Kayıtlı adreslerden seç |
| `addressPicker.placeholder` | Adres seçin |
| `addressPicker.hint` | Seçtiğiniz adres aşağıdaki alanlara kopyalanır; dilediğiniz gibi değiştirebilirsiniz. |
| `addressPicker.site` | Site adresi |

- [ ] **Step 2: Build `SavedAddressPicker`**: a `Field` with a `SearchableSelect`, no `name` (it is not part of the submitted form), no default value. Option text is `"<label> — <address line>"`, where the label is `t("addressPicker.site")` for `kind === "site"` and the saved address's own label otherwise. Choosing an option calls `onPick(option.fill)`.

- [ ] **Step 3: Wire the building form.** State: `const [picked, setPicked] = useState<{ fill: FilledAddress; n: number } | null>(null)`. Options: `pickOptions(selectedCustomerRecord?.addresses ?? [], selectedComplexRecord ?? null)`, where the two records are found in the customer and complex lists the form already loads; this requires tracking the selected complex id in state the way `customerId` is tracked. Render the picker directly above `AddressSelect`, with `key={selectedCustomer}` so changing the customer resets it. Wrap `AddressSelect`, the street and building number inputs and the address note textarea in one element with `key={picked?.n ?? 0}`, and give each a default from `picked?.fill` when it is set, otherwise from the record: `AddressSelect`'s `initial` (all six keys) and `location.initial` (`picked.fill.pin`, falling back to the record's pin when the fill has none), `street`, `building_number`, `address_note`. Changing the customer does not touch `picked`.

- [ ] **Step 4: Wire the site form** the same way, with `pickOptions(selectedCustomerRecord?.addresses ?? [], null)` and no address note.

- [ ] **Step 5: Run** `npm run typecheck && npm run lint:i18n && npm run lint:selects && npm run test`. Expected: PASS.

- [ ] **Step 6: Check it by hand** against the local API with a demo account:
  - New building, no customer chosen: no picker.
  - Choose a customer with two addresses: picker appears, nothing selected, fields empty.
  - Pick the billing address: province, district and neighbourhood all show it and can each be changed; street and number are filled; the pin moves if the address has one.
  - Type over the street, pick the other address: the street shows the second address's street.
  - Choose a site with an address: "Site adresi" is first; picking it fills the fields and leaves the pin alone.
  - Change the customer: picker is empty again and offers the new customer's addresses; the filled fields are still there.
  - Save, reopen the building: the saved values are the picked ones.
  - Same checks, minus the site entry, on a new site.

- [ ] **Step 7: Commit.**

```bash
git add src/components/forms/SavedAddressPicker.tsx src/screens/BuildingFormScreen.tsx src/screens/ComplexFormScreen.tsx messages/tr.json
git commit -m "GH-134-feat: pick a saved address in the site and building forms"
```

---

### Task 7: Verify and open the pull request

- [ ] **Step 1: Run `npm run verify`.** Expected: every step passes, including `lint:contract` against `shiftlush-api` main.

- [ ] **Step 2: Confirm the branch holds only this work.** `git log --oneline origin/main..HEAD` and `git diff --stat origin/main..HEAD` name only the files in this plan.

- [ ] **Step 3: Push and open the PR.**

```bash
git push -u origin GH-134
gh pr create --title "GH-134-feat: the customer address book" --body "<what changed, what was checked by hand, that it must deploy together with shiftlush-api#166>

Closes #134"
```

English, no AI attribution. Do not merge; the user decides when both sides go out.
