# Customer Address Book (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a customer a list of saved addresses in its own table, one of which can be the billing address, and move the customer's current address into it.

**Architecture:** A `CustomerAddress` model in `apps/customers` that mirrors `CustomerContact` end to end: the same base class, the same partial-unique-constraint-plus-demote pattern for the one-per-customer flag, the same flat viewset plus nested action under the customer. The customer's four address columns are copied into the new table by a data migration and then dropped; the customer read exposes the list as `addresses`.

**Tech Stack:** Django, Django REST Framework, drf-spectacular, pytest. SQLite locally, PostgreSQL in CI and production.

**Spec:** `docs/superpowers/specs/2026-11-19-customer-address-book-design.md`

**Companion plan:** `docs/superpowers/plans/2026-11-19-customer-address-book-web.md` (starts after this one is merged).

## Global Constraints

- Branch `GH-166`, worktree `~/projects/shiftlush-api-GH-166`. Commits: `GH-166-<type>: <English description>`. No AI attribution lines in commits or the PR.
- Code, comments, commit messages, PR text: English. The only Turkish in this plan is the migrated label `"Fatura adresi"`.
- Every migration and constraint must run on both SQLite and PostgreSQL.
- `openapi/v1.yaml` is generated with `make spec`, never edited by hand. Any commit that changes the contract includes the regenerated file.
- A breaking contract change must touch `CHANGELOG-API.md` in the same commit, or CI's `oasdiff` step fails.
- Write serializers use `StrictMixin` and list their fields one by one.
- Business-rule field errors are raised with `core.exceptions.field_error(field, ErrorCode.X)`.
- Before every commit: `make lint && make mypy`.

## Review Focus

1. **A customer whose old address had only a building or unit number.** The migrated row must be editable afterwards: the "must hold something" rule counts all five content fields. Test in Task 3 and Task 4.
2. **Marking a second address as billing.** The flag moves; the request succeeds with 200/201, never a 409 or an integrity error. Test in Task 3.
3. **Deleting the billing address, then marking another.** A soft-deleted row must not hold the slot. Test in Task 1.
4. **A PATCH that moves a billing address to another customer.** The demotion applies to the customer the row ends up under. Test in Task 3.
5. **The customer list with many addresses.** Query count does not grow with the number of customers or addresses. Test in Task 4.

---

### Task 1: The `CustomerAddress` model

**Files:**
- Modify: `apps/customers/models.py`
- Modify: `apps/customers/services.py`
- Create: `apps/customers/migrations/0008_customer_address.py` (generated)
- Modify: `tests/test_tenant_isolation.py` (the model registry near line 1135)
- Test: `tests/test_customer_addresses.py` (new)

**Interfaces:**
- Produces: `apps.customers.models.CustomerAddress` with fields `customer` (FK, `PROTECT`, `related_name="addresses"`), `label` (`CharField(max_length=60)`), `neighborhood` (FK `address.Neighborhood`, null, `PROTECT`, `related_name="customer_addresses"`), `street` (150, blank), `building_number` (20, blank), `unit_number` (20, blank), `address_note` (`TextField`, blank), `latitude`, `longitude` (`DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)`), `is_billing` (`BooleanField(default=False)`).
- Produces: `apps.customers.services.demote_other_billing(customer: Customer, keep_pk: UUID | None = None) -> None`.

- [ ] **Step 1: Write the failing tests** in `tests/test_customer_addresses.py`. Reuse the `firm_a` fixture shape from `tests/test_customer_api.py` (copy it; fixtures there are module-local).

```python
class TestTheBillingSlot:
    def test_two_live_billing_addresses_are_refused_by_the_database(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="A", is_billing=True
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                CustomerAddress.objects.create(
                    company=company, customer=customer, label="Two", street="B", is_billing=True
                )

    def test_a_deleted_billing_address_frees_the_slot(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            first = CustomerAddress.objects.create(
                company=company, customer=customer, label="One", street="A", is_billing=True
            )
            first.delete()  # soft delete
            CustomerAddress.objects.create(
                company=company, customer=customer, label="Two", street="B", is_billing=True
            )

    def test_demote_other_billing_clears_every_other_holder(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            old = CustomerAddress.objects.create(
                company=company, customer=customer, label="Old", street="A", is_billing=True
            )
            demote_other_billing(customer)
            old.refresh_from_db()
            assert old.is_billing is False

    def test_billing_sorts_first_then_label(self, firm_a):
        company, _, customer = firm_a
        with company_context(company.id):
            for label, billing in [("B", False), ("A", False), ("Z", True)]:
                CustomerAddress.objects.create(
                    company=company, customer=customer, label=label, street="x", is_billing=billing
                )
            assert [a.label for a in customer.addresses.all()] == ["Z", "A", "B"]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_customer_addresses.py -v`. Expected: FAIL, `ImportError: cannot import name 'CustomerAddress'`.

- [ ] **Step 3: Add the model** below `CustomerContact`. `Meta`: `db_table = "customer_address"`, `ordering = ["-is_billing", "label", "id"]`, one constraint `UniqueConstraint(fields=["customer"], condition=Q(is_billing=True, is_deleted=False), name="uq_customer_billing_address")`. `__str__` returns the label.

- [ ] **Step 4: Add `demote_other_billing`** to `apps/customers/services.py`, the same shape as `demote_other_primaries`: row by row with `save(update_fields=["is_billing", "updated_at"])` so the demotion is audited.

- [ ] **Step 5: Generate the migration.** `uv run python manage.py makemigrations customers -n customer_address`. Expected: `0008_customer_address.py` creating one model and one constraint, nothing else.

- [ ] **Step 6: Register the model** in the tenant-isolation registry in `tests/test_tenant_isolation.py` next to `"customers.CustomerContact"`, following whatever that list requires of an entry.

- [ ] **Step 7: Run** `uv run pytest tests/test_customer_addresses.py tests/test_tenant_isolation.py -v`. Expected: PASS.

- [ ] **Step 8: Commit.**

```bash
git add apps/customers tests/test_customer_addresses.py tests/test_tenant_isolation.py
git commit -m "GH-166-feat: add the CustomerAddress model and its billing slot"
```

---

### Task 2: `district_id` and `province_id` on every named address

**Files:**
- Modify: `core/serializers.py` (`AddressReadMixin`)
- Modify: `apps/properties/api/v1/serializers.py`, `apps/customers/api/v1/serializers.py` (the `fields` lists of the three read serializers that use the mixin)
- Modify: `openapi/v1.yaml` (generated), `CHANGELOG-API.md`
- Test: `tests/test_api_contract.py` (`TestNullableAddressNames`), `tests/test_address.py` or the existing building read test

**Interfaces:**
- Produces: read fields `district_id: int | null` (source `neighborhood.district_id`) and `province_id: int | null` (source `neighborhood.district.province_id`) on `AddressReadMixin`, both `read_only=True, allow_null=True`. Task 3's read serializer and the web plan rely on these names.

- [ ] **Step 1: Write the failing tests.** Extend the parametrisation of `TestNullableAddressNames::test_the_name_fields_are_declared_nullable` with `district_id` and `province_id` for `BuildingRead` and `ComplexRead`. Add one HTTP test beside the existing building read tests:

```python
def test_a_building_read_carries_the_ids_of_its_district_and_province(...):
    # building with a neighbourhood
    body = api_for(owner).get(reverse("building-detail", args=[building.id])).json()
    assert body["district_id"] == building.neighborhood.district_id
    assert body["province_id"] == building.neighborhood.district.province_id

def test_a_building_without_a_neighbourhood_has_null_ids(...):
    assert body["district_id"] is None and body["province_id"] is None
```

- [ ] **Step 2: Run them.** Expected: FAIL with `KeyError: 'district_id'`.

- [ ] **Step 3: Add the two fields** to `AddressReadMixin` and to the `fields` lists of `ComplexReadSerializer`, `BuildingReadSerializer` and `CustomerReadSerializer`, each directly after `neighborhood_name`. `ADDRESS_JOIN` already covers both; do not add joins.

- [ ] **Step 4: Regenerate the contract.** `make spec`. Expected: no warnings; the diff adds the two properties to three schemas.

- [ ] **Step 5: Add a line under "Added" in `CHANGELOG-API.md`:** address reads now carry `district_id` and `province_id`, so a client can fill a province and district picker from a record.

- [ ] **Step 6: Run** `uv run pytest tests/test_api_contract.py tests/test_n_plus_one.py -v` and the test file edited in Step 1. Expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add core/serializers.py apps openapi/v1.yaml CHANGELOG-API.md tests
git commit -m "GH-166-feat: serve district and province ids with a named address"
```

---

### Task 3: The address endpoints

**Files:**
- Modify: `core/error_codes.py`
- Modify: `apps/customers/api/v1/serializers.py`, `apps/customers/api/v1/views.py`, `apps/customers/api/v1/urls.py`
- Modify: `openapi/v1.yaml` (generated), `CHANGELOG-API.md`
- Test: `tests/test_customer_addresses.py`, `tests/test_field_error_codes.py`

**Interfaces:**
- Consumes: `CustomerAddress`, `demote_other_billing` (Task 1); `AddressReadMixin` with the id fields (Task 2).
- Produces:
  - `ErrorCode.ADDRESS_EMPTY = "ADDRESS_EMPTY", "An address needs a neighbourhood, a street, a number or a note"`.
  - `CustomerAddressReadSerializer` — fields, in order: `id`, `customer_id`, `label`, `neighborhood_id`, `neighborhood_name`, `district_id`, `district_name`, `province_id`, `province_name`, `street`, `building_number`, `unit_number`, `address_note`, `latitude`, `longitude`, `is_billing`, `created_at`, `updated_at`.
  - `CustomerAddressWriteSerializer` — `customer`, `label`, `neighborhood`, `street`, `building_number`, `unit_number`, `address_note`, `latitude`, `longitude`, `is_billing`.
  - `CustomerAddressNestedWriteSerializer` — the same without `customer`.
  - Routes: `customer-addresses` (basename `customer-address`), and the `addresses` action on `CustomerViewSet` (`GET, POST /customers/{id}/addresses/`).

- [ ] **Step 1: Write the failing tests** in `tests/test_customer_addresses.py`.

```python
class TestTheEndpoints:
    def test_create_under_the_customer(self, firm_a):
        _, owner, customer = firm_a
        response = api_for(owner).post(
            f"/api/v1/customers/{customer.id}/addresses/",
            {"label": "Head office", "street": "Ataturk Cd.", "is_billing": True},
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["customer_id"] == str(customer.id)
        assert body["is_billing"] is True
        assert body["district_id"] is None

    def test_the_nested_create_refuses_a_customer_in_the_body(self, firm_a): ...
        # {"customer": <id>, "label": "x", "street": "y"} -> 400, error on "customer"

    def test_list_under_the_customer_is_billing_first(self, firm_a): ...
        # labels B (plain), A (billing) -> results labels ["A", "B"]

    def test_the_flat_list_filters_by_customer_and_is_billing(self, firm_a): ...

    def test_marking_a_second_address_billing_moves_the_flag(self, firm_a):
        # create One (billing), POST Two with is_billing=True -> 201
        # One.is_billing is False, Two.is_billing is True
        # then PATCH One {"is_billing": True} -> 200, Two.is_billing is False

    def test_a_billing_address_moved_to_another_customer_demotes_there(self, firm_a):
        # second customer in the same company with its own billing address;
        # PATCH the first customer's billing address {"customer": <second id>} -> 200;
        # the second customer's old billing address is no longer billing

    def test_delete_is_soft_and_frees_the_billing_slot(self, firm_a): ...
        # DELETE -> 204; row still exists with is_deleted=True; a new billing address -> 201

    def test_another_companys_customer_is_a_404(self, firm_a, firm_b): ...
        # A's owner GET/POST /customers/<B's customer>/addresses/ -> 404
        # A's owner POST /customer-addresses/ with B's customer id -> 400

class TestAnAddressMustHoldSomething:
    def test_a_label_alone_is_refused(self, firm_a):
        # {"label": "Empty"} -> 400, body error for field "label" has code "ADDRESS_EMPTY"

    @pytest.mark.parametrize(
        "field", ["street", "building_number", "unit_number", "address_note"]
    )
    def test_any_one_content_field_is_enough(self, firm_a, field): ...  # -> 201

    def test_a_patch_that_empties_the_last_content_field_is_refused(self, firm_a):
        # address with only street; PATCH {"street": ""} -> 400 ADDRESS_EMPTY

    def test_an_address_with_only_a_unit_number_can_be_relabelled(self, firm_a):
        # row created with only unit_number; PATCH {"label": "New"} -> 200
```

Read how an existing test in `tests/test_field_error_codes.py` asserts a field's code and assert `ADDRESS_EMPTY` the same way. Use `reverse()` with the router names where the existing customer tests do.

- [ ] **Step 2: Run** `uv run pytest tests/test_customer_addresses.py -v`. Expected: the new classes FAIL with 404s.

- [ ] **Step 3: Add `ADDRESS_EMPTY`** to `core/error_codes.py` in the validation section.

- [ ] **Step 4: Write the three serializers.** The write serializer's `validate` computes each of `neighborhood`, `street`, `building_number`, `unit_number`, `address_note` as "what the field will hold once this request is applied" (the `_effective` idea in `CustomerWriteSerializer`) and raises `field_error("label", ErrorCode.ADDRESS_EMPTY)` when all five are empty. `create` and `update` are `@transaction.atomic` and call `demote_other_billing` before the write when the row will be billing; on update the customer is `validated_data.get("customer", instance.customer)` and `keep_pk=instance.pk`. No latitude/longitude pair check.

- [ ] **Step 5: Add `CustomerAddressViewSet`** (`resource = "customer"`, `customer_path = "customer_id"`, `filterset_fields = ["customer", "is_billing"]`, base queryset `select_related("customer", *ADDRESS_JOIN)`) and the `addresses` action on `CustomerViewSet`, a copy of `contacts`/`_create_contact` with the address serializers, both `extend_schema` blocks included. Register `customer-addresses` in `urls.py`.

- [ ] **Step 6: Regenerate the contract and announce it.** `make spec` (no warnings), then an "Added" entry in `CHANGELOG-API.md` for the two routes and the `ADDRESS_EMPTY` code.

- [ ] **Step 7: Run** `uv run pytest tests/test_customer_addresses.py tests/test_api_contract.py tests/test_field_error_codes.py tests/test_permissions.py -v`. Expected: PASS. If `test_permissions.py` or `test_audit.py` enumerate routes or models, add the new one the way contacts are listed.

- [ ] **Step 8: Commit.**

```bash
git add core/error_codes.py apps/customers openapi/v1.yaml CHANGELOG-API.md tests
git commit -m "GH-166-feat: customer address endpoints"
```

---

### Task 4: Move the customer's address into the book

**Files:**
- Create: `apps/customers/migrations/0009_move_customer_address.py` (hand-written data migration)
- Create: `apps/customers/migrations/0010_drop_customer_address_columns.py` (generated)
- Modify: `apps/customers/models.py` (remove `neighborhood`, `street`, `building_number`, `unit_number` from `Customer`)
- Modify: `apps/customers/api/v1/serializers.py`, `apps/customers/api/v1/views.py`
- Modify: `core/demo.py`, and `core/management/commands/seed_demo_data.py` / `core/data/demo.json` wherever they set a customer address
- Modify: `openapi/v1.yaml` (generated), `CHANGELOG-API.md`
- Test: `tests/test_customer_address_migration.py` (new), `tests/test_customer_api.py`, `tests/test_n_plus_one.py`, `tests/test_demo_data.py`

**Interfaces:**
- Consumes: `CustomerAddress`, `CustomerAddressReadSerializer`.
- Produces: `CustomerReadSerializer` without `neighborhood_id`, `neighborhood_name`, `district_id`, `district_name`, `province_id`, `province_name`, `street`, `building_number`, `unit_number`, and with `addresses: CustomerAddressRead[]` placed after `contacts`. `CustomerWriteSerializer` without `neighborhood`, `street`, `building_number`, `unit_number`. `CustomerReadSerializer` no longer inherits `AddressReadMixin`.

- [ ] **Step 1: Write the failing migration test** in `tests/test_customer_address_migration.py`, using `django.db.migrations.executor.MigrationExecutor` to migrate `customers` to `0008`, insert rows through the historical models, migrate to `0009`, and read back.

```python
# three customers at 0008:
#   full:  neighborhood=<n>, street="Ataturk Cd.", building_number="12", unit_number="3"
#   unit:  only unit_number="5"
#   empty: nothing
# after 0009:
assert addresses_of(full) == [
    {"label": "Fatura adresi", "is_billing": True, "neighborhood_id": n.id,
     "street": "Ataturk Cd.", "building_number": "12", "unit_number": "3",
     "company_id": full.company_id}
]
assert len(addresses_of(unit)) == 1 and addresses_of(unit)[0]["unit_number"] == "5"
assert addresses_of(empty) == []
# migrating back to 0008 puts the billing address's four values on the customer again
```

Mark it `@pytest.mark.django_db(transaction=True)` and migrate forward to the latest state in a `finally`, so later tests see the full schema.

- [ ] **Step 2: Run it.** Expected: FAIL, migration `0009` does not exist.

- [ ] **Step 3: Write `0009_move_customer_address.py`.** `RunPython(forwards, backwards)` on historical models from `apps.get_model`. Forwards: one `CustomerAddress` per customer (soft-deleted customers included) with any of the four fields set, built in a list and written with `bulk_create`; `label="Fatura adresi"`, `is_billing=True`, `company_id` and `is_deleted` copied from the customer. Backwards: for each live billing address, copy the four values onto its customer.

- [ ] **Step 4: Run the migration test.** Expected: PASS.

- [ ] **Step 5: Write the failing API tests** in `tests/test_customer_api.py`:

```python
def test_the_customer_read_lists_its_addresses(self, firm_a):
    # one billing address on the customer
    body = api_for(owner).get(reverse("customer-detail", args=[customer.id])).json()
    assert [a["label"] for a in body["addresses"]] == ["Head office"]
    assert "street" not in body and "neighborhood_id" not in body

def test_the_customer_write_refuses_address_fields(self, firm_a):
    response = api_for(owner).patch(
        reverse("customer-detail", args=[customer.id]), {"street": "x"}, format="json"
    )
    assert response.status_code == 400
    assert "street" in response.json()[...]  # the envelope key existing tests use
```

and in `tests/test_n_plus_one.py`, a customer-list test in the style of the contract one: the query count for 1 customer with 1 address equals the count for 3 customers with 3 addresses each.

- [ ] **Step 6: Remove the four columns** from `Customer`, generate `0010` with `makemigrations customers -n drop_customer_address_columns` (expected: four `RemoveField` operations and nothing else), and update the serializers as listed under Interfaces. In `CustomerViewSet`'s queryset, drop the address `select_related` and prefetch addresses with their joins: `Prefetch("addresses", queryset=CustomerAddress.objects.select_related(*ADDRESS_JOIN))`. Fix any other reader the type checker or `grep -rn "customer\.\(neighborhood\|street\)" apps core` turns up.

- [ ] **Step 7: Update the demo data.** In `core/demo.py`, create each customer's address as a `CustomerAddress` (`label="Fatura adresi"`, `is_billing=True`) and read the site and building addresses from it instead of from the customer (lines 379-437 today). Give every fourth demo customer a second address labelled `"Şantiye girişi"` with `is_billing=False`. Update `tests/test_demo_data.py`'s `province_of` helper to read a customer's province from its billing address.

- [ ] **Step 8: Regenerate the contract and announce the break.** `make spec`, then under "Changed" in `CHANGELOG-API.md`: **Breaking** — the customer no longer carries `neighborhood`, `street`, `building_number`, `unit_number` in either direction; read `addresses` and write through `/customers/{id}/addresses`; existing addresses were migrated as the billing address. This file and `openapi/v1.yaml` go in the same commit.

- [ ] **Step 9: Run the whole suite.** `make test`. Expected: PASS. Then `make lint && make mypy && make check`.

- [ ] **Step 10: Commit.**

```bash
git add apps/customers core openapi/v1.yaml CHANGELOG-API.md tests
git commit -m "GH-166-feat: move the customer's address into the address book"
```

---

### Task 5: PostgreSQL run and the pull request

**Files:** none new.

- [ ] **Step 1: Run the suite on PostgreSQL** if the services are available: `make services && make test-pg`. Expected: PASS. If Docker is not running, say so in the PR and rely on CI, which runs this.

- [ ] **Step 2: Confirm the branch holds only this work.** `git log --oneline origin/main..HEAD` shows the design, the plans and the four feature commits; `git diff --stat origin/main..HEAD` touches only the files named in this plan.

- [ ] **Step 3: Push and open the PR.**

```bash
git push -u origin GH-166
gh pr create --title "GH-166-feat: the customer address book" --body "<what changed, the breaking change and its changelog entry, how it was tested>

Closes #166"
```

The body is English and carries no AI attribution. Do not merge: the user decides when, and the web change (shiftlush-web#134) should be ready to deploy with it.
