# Maintenance Visit Record — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a technician (or Owner/Admin) record a monthly maintenance visit against an elevator, with a fixed checklist, and have the elevator's next-due date recalculate automatically.

**Architecture:** A new Django app `apps.maintenance` with two models (`MaintenanceVisit`, `MaintenanceVisitItem`), following the existing `Contract`/`ContractElevator` shape exactly (both `CompanyOwnedModel`, `PROTECT` foreign keys, a service module for business logic, a `TenantViewSet` subclass for the API). Two small, targeted changes ride along: `Elevator` gains two date fields, and `Attachment` gains one narrow permission carve-out so a technician can attach an optional photo to their own visit without gaining write access to every attachment category.

**Tech Stack:** Django 6.1, DRF, `uv`, pytest + pytest-django, ruff, mypy (strict-ish per `pyproject.toml` overrides).

**Spec:** `docs/superpowers/specs/2026-09-12-maintenance-visit-design.md`

One deviation from that spec, decided while mapping out files below: the spec
describes `items` as a **writable nested serializer** on `MaintenanceVisit`.
Every existing parent/child relationship in this codebase (`Contract` /
`ContractElevator`) instead creates the parent first and manages children
through a separate action (`add-elevators`). Task 7 below follows that
established pattern instead: `POST /maintenance-visits/{id}/set-checklist/`
replaces the full set of checklist rows in one call. Functionally
equivalent for the frontend (create, then one more call with the checklist,
same as `Contract` + `add-elevators` already works today); simpler to
implement and consistent with the rest of the API. The spec file is updated
in Task 9 to match.

## Global Constraints

- Every business model is a `CompanyOwnedModel` (soft delete + tenant scope
  built in) unless there is a specific reason not to be — there is none here.
- Every foreign key between business models uses `on_delete=models.PROTECT`.
  Nothing in this codebase hard-deletes in normal operation; deletion is
  soft-delete via `CompanyOwnedModel.delete()`.
- Every `UniqueConstraint` on a `SoftDeleteModel` subclass carries
  `condition=models.Q(is_deleted=False)` — a soft-deleted row must not keep a
  business key reserved forever.
- Every list-serving `TenantViewSet` subclass sets an explicit `ordering`
  attribute ending in `id`/`pk`, not just `Meta.ordering` — see SL-118 (#79):
  an aggregate `.annotate(Count(...))` silently drops a model's default
  ordering, and the viewset attribute is what survives that.
- Code, identifiers, and `TextChoices` labels are English (see
  `apps/users/models.py:18-19`: "Stored in English; the Turkish labels live
  in the frontend translation file"). Nothing here touches `locale/` or
  templates, so no Turkish text is written in this plan at all.
- Run `make lint && make test && make check && make mypy` before every
  commit that is meant to be a checkpoint, and always as the last step.
- `openapi/v1.yaml` is generated, never hand-edited (`make spec`), and CI
  fails the build on a spectacular warning — regenerate it in Task 9 and
  fix anything it flags rather than editing the file directly.

---

## File Structure

New:
- `apps/maintenance/__init__.py`
- `apps/maintenance/apps.py` — `AppConfig`
- `apps/maintenance/models.py` — `VisitStatus`, `ChecklistItem`, `ChecklistResult` enums; `MaintenanceVisit`; `MaintenanceVisitItem`
- `apps/maintenance/migrations/__init__.py`, `0001_initial.py` (generated, Task 2)
- `apps/maintenance/services.py` — `ensure_active_contract`, `recalculate_next_maintenance`, `set_checklist`
- `apps/maintenance/api/__init__.py`
- `apps/maintenance/api/v1/__init__.py`
- `apps/maintenance/api/v1/serializers.py` — `MaintenanceVisitItemSerializer`, `MaintenanceVisitReadSerializer`, `MaintenanceVisitWriteSerializer`, `SetChecklistSerializer`
- `apps/maintenance/api/v1/views.py` — `MaintenanceVisitViewSet`
- `apps/maintenance/api/v1/urls.py` — router
- `apps/attachments/api/v1/permissions.py` — `AttachmentPermission`
- `tests/test_maintenance_visits.py`
- `tests/test_maintenance_permissions.py`
- `tests/test_maintenance_attachments.py`

Modified:
- `config/settings/base.py` — register `apps.maintenance` in `INSTALLED_APPS`
- `config/urls.py` — include `apps.maintenance.api.v1.urls`
- `apps/elevators/models.py` — add `last_maintenance_date`, `next_maintenance_date`
- `apps/elevators/migrations/` — new migration (generated, Task 1)
- `apps/attachments/models.py` — add `ObjectType.MAINTENANCE_VISIT`
- `apps/attachments/migrations/` — new migration (generated, Task 3)
- `apps/attachments/services.py` — add `MAINTENANCE_VISIT` to `TARGET_MODELS`; add `peek_object_type_and_category`
- `apps/attachments/api/v1/views.py` — swap `permission_classes` to the new `AttachmentPermission`
- `core/permissions.py` — add `maintenance_visit` and `maintenance_visit_edit` to `MATRIX`
- `core/error_codes.py` — add `NO_ACTIVE_CONTRACT_FOR_ELEVATOR`
- `openapi/v1.yaml` — regenerated (Task 9)
- `docs/superpowers/specs/2026-09-12-maintenance-visit-design.md` — updated to match the `set-checklist` action (Task 9)

---

### Task 1: `Elevator` gains `last_maintenance_date` / `next_maintenance_date`

**Files:**
- Modify: `apps/elevators/models.py`
- Test: `tests/test_maintenance_visits.py` (new file, started here)

**Interfaces:**
- Produces: `Elevator.last_maintenance_date: date | None`, `Elevator.next_maintenance_date: date | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_maintenance_visits.py`:

```python
"""Maintenance visit records - specification in
docs/superpowers/specs/2026-09-12-maintenance-visit-design.md.
"""

from __future__ import annotations

from apps.elevators.models import Elevator


class TestElevatorMaintenanceFields:
    def test_new_fields_default_to_null(self, db):
        from datetime import date

        from apps.companies.models import Company
        from apps.customers.models import Customer, CustomerType
        from apps.properties.models import Building, BuildingType
        from core.context import company_context, system_context

        with system_context():
            company = Company.objects.create(legal_name="Firm", display_name="Firm")
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Cust"
            )
            building = Building.objects.create(
                company=company,
                customer=customer,
                name="B1",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
            )
            elevator = Elevator.objects.create(
                company=company, building=building, qr_token="a" * 24
            )

        assert elevator.last_maintenance_date is None
        assert elevator.next_maintenance_date is None
        assert isinstance(elevator.maintenance_interval_days, int)
        assert date  # imported for readability of the test body above
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: FAIL — `AttributeError` (or Django raising on the field not
existing) referencing `last_maintenance_date`.

- [ ] **Step 3: Add the fields**

In `apps/elevators/models.py`, right after the existing periodic-inspection
block (after `inspection_report_number`, before the `# Status` comment),
add:

```python
    # Monthly maintenance visits — apps.maintenance owns writing these, via
    # recalculate_next_maintenance(). Never set directly from a serializer.
    last_maintenance_date = models.DateField(null=True, blank=True)
    next_maintenance_date = models.DateField(null=True, blank=True)
```

Add an index alongside the existing `next_inspection_date` one, inside
`Meta.indexes`:

```python
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["building"]),
            models.Index(fields=["next_inspection_date"]),
            models.Index(fields=["next_maintenance_date"]),
        ]
```

- [ ] **Step 4: Generate and apply the migration**

Run: `uv run python manage.py makemigrations elevators`
Expected: a new file under `apps/elevators/migrations/` adding the two
fields and the index. Open it and confirm it only touches `Elevator`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/elevators/models.py apps/elevators/migrations/ tests/test_maintenance_visits.py
git commit -m "SL-119-feat: add last/next maintenance date fields to Elevator"
```

---

### Task 2: `apps.maintenance` app skeleton — `MaintenanceVisit` and `MaintenanceVisitItem`

**Files:**
- Create: `apps/maintenance/__init__.py`, `apps/maintenance/apps.py`, `apps/maintenance/models.py`
- Modify: `config/settings/base.py`
- Test: `tests/test_maintenance_visits.py`

**Interfaces:**
- Consumes: `core.models.CompanyOwnedModel`, `apps.elevators.models.Elevator`, `apps.contracts.models.Contract`, `apps.users.models.User`
- Produces:
  - `VisitStatus` (`TextChoices`): `COMPLETED`, `INCOMPLETE`, `ACCESS_DENIED`
  - `ChecklistItem` (`TextChoices`): 16 members, listed below
  - `ChecklistResult` (`TextChoices`): `OK`, `ATTENTION_NEEDED`, `NOT_APPLICABLE`
  - `MaintenanceVisit(company, elevator, contract, technician, visit_date, status, status_note, work_notes, parts_replaced, ...)`
  - `MaintenanceVisitItem(company, visit, item, result, note, ...)`, related name `items` on `MaintenanceVisit`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maintenance_visits.py`:

```python
from datetime import date, timedelta

import pytest

from apps.companies.models import Company
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"


@pytest.fixture
def visit_fixture(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Cust"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="B1",
            type=BuildingType.RESIDENTIAL,
            address_note="x",
        )
        elevator = Elevator.objects.create(company=company, building=building, qr_token="b" * 24)
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number="C-1",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() + timedelta(days=335),
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=date.today()
        )
    return company, owner, elevator, contract


class TestMaintenanceVisitModel:
    def test_create_visit_with_checklist_items(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            item = MaintenanceVisitItem.objects.create(
                company=company,
                visit=visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.OK,
            )

            # Inside company_context: MaintenanceVisitItem is a
            # CompanyOwnedModel, so the reverse manager `visit.items` is
            # tenant-scoped and returns nothing once the context exits.
            assert visit.items.count() == 1
            assert item.visit_id == visit.id

    def test_same_item_twice_on_one_visit_is_rejected(self, visit_fixture):
        from django.db import IntegrityError

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            MaintenanceVisitItem.objects.create(
                company=company, visit=visit, item=ChecklistItem.BRAKE_SYSTEM, result=ChecklistResult.OK
            )
            with pytest.raises(IntegrityError):
                MaintenanceVisitItem.objects.create(
                    company=company,
                    visit=visit,
                    item=ChecklistItem.BRAKE_SYSTEM,
                    result=ChecklistResult.ATTENTION_NEEDED,
                )
```

Add `from apps.elevators.models import Elevator` to the existing imports at
the top of the file (it is already imported for Task 1's test, so only add
it if it is not already there).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.maintenance'`

- [ ] **Step 3: Create the app**

`apps/maintenance/__init__.py`: empty file.

`apps/maintenance/apps.py`:

```python
from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.maintenance"
```

`apps/maintenance/models.py`:

```python
"""The record a technician fills in for a routine monthly maintenance visit.

Not the periodic inspection module: Elevator.last_inspection_date and its
neighbours describe an independent third-party statutory inspection, a
different legal event from the company's own technician doing the monthly
visit this module covers. See the design doc for the full distinction.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class VisitStatus(models.TextChoices):
    COMPLETED = "completed", "Completed"
    INCOMPLETE = "incomplete", "Incomplete"
    ACCESS_DENIED = "access_denied", "Access denied"


class ChecklistItem(models.TextChoices):
    BRAKE_SYSTEM = "brake_system", "Brake system"
    HOISTING_ROPES = "hoisting_ropes", "Hoisting ropes and cables"
    LANDING_DOORS = "landing_doors", "Landing doors and locks"
    CAR_DOOR_SAFETY_EDGE = "car_door_safety_edge", "Car door and safety edge"
    LIMIT_SWITCHES = "limit_switches", "Limit switches"
    GUIDE_RAIL_LUBRICATION = "guide_rail_lubrication", "Guide rail lubrication"
    CAR_LIGHTING = "car_lighting", "Car lighting"
    EMERGENCY_LIGHTING = "emergency_lighting", "Emergency lighting"
    EMERGENCY_ALARM_INTERCOM = "emergency_alarm_intercom", "Emergency alarm / intercom"
    CONTROL_PANEL_BUTTONS = "control_panel_buttons", "Control panel and call buttons"
    LEVELING_ACCURACY = "leveling_accuracy", "Floor leveling accuracy"
    OVERLOAD_DEVICE = "overload_device", "Overload sensor"
    AUTOMATIC_RESCUE_DEVICE = "automatic_rescue_device", "Automatic rescue device (ARD) test"
    PIT_CONDITION = "pit_condition", "Pit condition and cleanliness"
    MACHINE_ROOM_CONDITION = "machine_room_condition", "Machine room condition and ventilation"
    EARTHING_GROUNDING = "earthing_grounding", "Earthing / grounding"


class ChecklistResult(models.TextChoices):
    OK = "ok", "OK"
    ATTENTION_NEEDED = "attention_needed", "Attention needed"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


class MaintenanceVisit(CompanyOwnedModel):
    elevator = models.ForeignKey(
        "elevators.Elevator", on_delete=models.PROTECT, related_name="maintenance_visits"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.PROTECT, related_name="maintenance_visits"
    )
    technician = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="maintenance_visits_performed"
    )
    visit_date = models.DateField()
    status = models.CharField(max_length=20, choices=VisitStatus.choices)
    status_note = models.TextField(blank=True)
    work_notes = models.TextField(blank=True)
    # Free text: nothing today queries which parts were used across the
    # fleet. A dedicated parts/inventory model is not built until something
    # does (YAGNI) - see the design doc.
    parts_replaced = models.TextField(blank=True)

    class Meta:
        db_table = "maintenance_visit"
        ordering = ["-visit_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=VisitStatus.values),
                name="maintenance_visit_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["elevator", "-visit_date"]),
            models.Index(fields=["company", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.elevator_id} / {self.visit_date}"


class MaintenanceVisitItem(CompanyOwnedModel):
    visit = models.ForeignKey(MaintenanceVisit, on_delete=models.PROTECT, related_name="items")
    item = models.CharField(max_length=32, choices=ChecklistItem.choices)
    result = models.CharField(max_length=20, choices=ChecklistResult.choices)
    note = models.TextField(blank=True)

    class Meta:
        db_table = "maintenance_visit_item"
        ordering = ["visit_id", "item"]
        constraints = [
            models.UniqueConstraint(
                fields=["visit", "item"],
                condition=models.Q(is_deleted=False),
                name="uq_maintenance_visit_item",
            ),
            models.CheckConstraint(
                condition=models.Q(item__in=ChecklistItem.values), name="maintenance_visit_item_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(result__in=ChecklistResult.values),
                name="maintenance_visit_item_result_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.visit_id} / {self.item}"
```

In `config/settings/base.py`, add `"apps.maintenance",` to `INSTALLED_APPS`
right after `"apps.attachments",` and before `"apps.audit",` (matches the
existing declaration order — each app after the ones it depends on).

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations maintenance`
Expected: `apps/maintenance/migrations/0001_initial.py` creating both
tables.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add apps/maintenance config/settings/base.py tests/test_maintenance_visits.py
git commit -m "SL-119-feat: add the maintenance app with MaintenanceVisit and MaintenanceVisitItem"
```

---

### Task 3: `Attachment` learns about `maintenance_visit`

**Files:**
- Modify: `apps/attachments/models.py`, `apps/attachments/services.py`
- Test: `tests/test_maintenance_attachments.py` (new file)

**Interfaces:**
- Consumes: `apps.maintenance.models.MaintenanceVisit` (Task 2)
- Produces: `ObjectType.MAINTENANCE_VISIT`; `attachments.services.TARGET_MODELS["maintenance_visit"] == "maintenance.MaintenanceVisit"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_maintenance_attachments.py`:

```python
"""Attachments against a maintenance visit - see apps.attachments.services._target_exists."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.attachments.models import ObjectType
from apps.attachments.services import TARGET_MODELS, _target_exists
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.properties.models import Building, BuildingType
from apps.users.services import register_company
from core.context import company_context

PASSWORD = "correct-horse-battery"


@pytest.fixture
def a_visit(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner2@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Cust"
        )
        building = Building.objects.create(
            company=company, customer=customer, name="B1", type=BuildingType.RESIDENTIAL, address_note="x"
        )
        elevator = Elevator.objects.create(company=company, building=building, qr_token="c" * 24)
        contract = Contract.objects.create(
            company=company,
            customer=customer,
            contract_number="C-2",
            status=ContractStatus.ACTIVE,
            scope=Scope.MAINTENANCE_ONLY,
            start_date=date.today() - timedelta(days=10),
            end_date=date.today() + timedelta(days=355),
            pricing_type=PricingType.FLAT,
            monthly_fee="1000.00",
            vat_rate="20.00",
        )
        ContractElevator.objects.create(
            company=company, contract=contract, elevator=elevator, added_at=date.today()
        )
        visit = MaintenanceVisit.objects.create(
            company=company,
            elevator=elevator,
            contract=contract,
            technician=owner,
            visit_date=date.today(),
            status=VisitStatus.COMPLETED,
        )
    return company, visit


class TestMaintenanceVisitIsAnAttachmentTarget:
    def test_object_type_is_registered(self):
        assert "maintenance_visit" in TARGET_MODELS
        assert TARGET_MODELS[ObjectType.MAINTENANCE_VISIT] == "maintenance.MaintenanceVisit"

    def test_target_exists_finds_a_real_visit(self, a_visit):
        company, visit = a_visit
        assert _target_exists(company.id, ObjectType.MAINTENANCE_VISIT, visit.id)

    def test_target_exists_rejects_another_companys_visit(self, a_visit):
        import uuid

        _, visit = a_visit
        assert not _target_exists(uuid.uuid4(), ObjectType.MAINTENANCE_VISIT, visit.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_attachments.py -v`
Expected: FAIL — `AttributeError: MAINTENANCE_VISIT` or a `KeyError` on
`TARGET_MODELS`.

- [ ] **Step 3: Add the object type**

In `apps/attachments/models.py`, add to `ObjectType`:

```python
class ObjectType(models.TextChoices):
    ELEVATOR = "elevator", "Elevator"
    BUILDING = "building", "Building"
    CONTRACT = "contract", "Contract"
    CUSTOMER = "customer", "Customer"
    COMPANY = "company", "Company"
    USER = "user", "User"
    MAINTENANCE_VISIT = "maintenance_visit", "Maintenance visit"
```

In `apps/attachments/services.py`, add to `TARGET_MODELS`:

```python
TARGET_MODELS: dict[str, str] = {
    ObjectType.ELEVATOR: "elevators.Elevator",
    ObjectType.BUILDING: "properties.Building",
    ObjectType.CONTRACT: "contracts.Contract",
    ObjectType.CUSTOMER: "customers.Customer",
    ObjectType.COMPANY: "companies.Company",
    ObjectType.USER: "users.User",
    ObjectType.MAINTENANCE_VISIT: "maintenance.MaintenanceVisit",
}
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations attachments`
Expected: a migration altering the `object_type` choices/check constraint.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_attachments.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/attachments tests/test_maintenance_attachments.py
git commit -m "SL-119-feat: let attachments target a maintenance visit"
```

---

### Task 4: Business logic — active-contract check and next-maintenance recalculation

**Files:**
- Create: `apps/maintenance/services.py`
- Modify: `core/error_codes.py`
- Test: `tests/test_maintenance_visits.py`

**Interfaces:**
- Consumes: `MaintenanceVisit`, `MaintenanceVisitItem`, `VisitStatus`, `ChecklistItem`, `ChecklistResult` (Task 2); `Contract`, `ContractElevator`, `ContractStatus` (existing); `Elevator` (existing + Task 1 fields); `core.exceptions.BusinessRuleError`; `core.error_codes.ErrorCode`
- Produces:
  - `ensure_active_contract(*, elevator: Elevator, contract: Contract) -> None` — raises `BusinessRuleError(ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR)` if the contract is not `ACTIVE` or does not currently cover the elevator
  - `recalculate_next_maintenance(elevator: Elevator) -> None`
  - `set_checklist(*, visit: MaintenanceVisit, items: list[dict[str, str]]) -> list[MaintenanceVisitItem]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_maintenance_visits.py`:

```python
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError


class TestEnsureActiveContract:
    def test_rejects_a_contract_that_does_not_cover_the_elevator(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            other_customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Other"
            )
            other_contract = Contract.objects.create(
                company=company,
                customer=other_customer,
                contract_number="C-3",
                status=ContractStatus.ACTIVE,
                scope=Scope.MAINTENANCE_ONLY,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=365),
                pricing_type=PricingType.FLAT,
                monthly_fee="500.00",
                vat_rate="20.00",
            )
            with pytest.raises(BusinessRuleError) as exc:
                services.ensure_active_contract(elevator=elevator, contract=other_contract)
        assert exc.value.detail.code == ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR.value

    def test_rejects_a_non_active_contract(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            contract.status = ContractStatus.DRAFT
            contract.save(update_fields=["status"])
            with pytest.raises(BusinessRuleError):
                services.ensure_active_contract(elevator=elevator, contract=contract)

    def test_accepts_an_active_contract_covering_the_elevator(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            services.ensure_active_contract(elevator=elevator, contract=contract)  # no raise


class TestRecalculateNextMaintenance:
    def test_no_completed_visit_leaves_dates_null(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            services.recalculate_next_maintenance(elevator)
        elevator.refresh_from_db()
        assert elevator.last_maintenance_date is None
        assert elevator.next_maintenance_date is None

    def test_completed_visit_sets_both_dates(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        visit_date = date.today() - timedelta(days=5)
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=visit_date,
                status=VisitStatus.COMPLETED,
            )
            services.recalculate_next_maintenance(elevator)
        elevator.refresh_from_db()
        assert elevator.last_maintenance_date == visit_date
        assert elevator.next_maintenance_date == visit_date + timedelta(
            days=elevator.maintenance_interval_days
        )

    def test_incomplete_visit_does_not_move_the_date(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.ACCESS_DENIED,
            )
            services.recalculate_next_maintenance(elevator)
        elevator.refresh_from_db()
        assert elevator.last_maintenance_date is None

    def test_uses_the_latest_completed_visit_by_date_not_by_creation_order(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        older = date.today() - timedelta(days=40)
        newer = date.today() - timedelta(days=2)
        with company_context(company.id):
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=newer,
                status=VisitStatus.COMPLETED,
            )
            # Entered second, but an earlier visit date - a backdated entry.
            MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=older,
                status=VisitStatus.COMPLETED,
            )
            services.recalculate_next_maintenance(elevator)
        elevator.refresh_from_db()
        assert elevator.last_maintenance_date == newer


class TestSetChecklist:
    def test_replaces_the_full_set(self, visit_fixture):
        from apps.maintenance import services

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            services.set_checklist(
                visit=visit,
                items=[{"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK}],
            )
            services.set_checklist(
                visit=visit,
                items=[
                    {"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.ATTENTION_NEEDED},
                    {"item": ChecklistItem.HOISTING_ROPES, "result": ChecklistResult.OK},
                ],
            )
        assert visit.items.count() == 2
        brake = visit.items.get(item=ChecklistItem.BRAKE_SYSTEM)
        assert brake.result == ChecklistResult.ATTENTION_NEEDED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.maintenance.services'`

- [ ] **Step 3: Add the error code**

In `core/error_codes.py`, in the "Business rules" section (next to
`ELEVATOR_ALREADY_CONTRACTED`), add:

```python
    NO_ACTIVE_CONTRACT_FOR_ELEVATOR = (
        "NO_ACTIVE_CONTRACT_FOR_ELEVATOR",
        "The named contract is not active or does not cover this elevator",
    )
```

- [ ] **Step 4: Write `apps/maintenance/services.py`**

```python
"""Business rules for a maintenance visit.

Two things live here rather than in the serializer: whether the named
contract actually backs this elevator (a question about the world, not
about the shape of the request), and keeping Elevator.next_maintenance_date
correct as visits are created, edited, and deleted - always recomputed from
the surviving rows, never incremented in place, so a backdated or deleted
visit cannot leave a stale date behind.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TypedDict

from django.db import transaction

from apps.contracts.models import Contract, ContractElevator, ContractStatus
from apps.elevators.models import Elevator
from apps.maintenance.models import MaintenanceVisit, MaintenanceVisitItem, VisitStatus
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError


class ChecklistEntry(TypedDict):
    item: str
    result: str
    note: str | None


def ensure_active_contract(*, elevator: Elevator, contract: Contract) -> None:
    if contract.status != ContractStatus.ACTIVE:
        raise BusinessRuleError(ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR)
    covers = ContractElevator.objects.filter(
        contract=contract, elevator=elevator, removed_at__isnull=True
    ).exists()
    if not covers:
        raise BusinessRuleError(ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR)


def recalculate_next_maintenance(elevator: Elevator) -> None:
    latest = (
        MaintenanceVisit.objects.filter(elevator=elevator, status=VisitStatus.COMPLETED)
        .order_by("-visit_date", "-id")
        .first()
    )
    if latest is None:
        elevator.last_maintenance_date = None
        elevator.next_maintenance_date = None
    else:
        elevator.last_maintenance_date = latest.visit_date
        elevator.next_maintenance_date = latest.visit_date + timedelta(
            days=elevator.maintenance_interval_days
        )
    elevator.save(update_fields=["last_maintenance_date", "next_maintenance_date", "updated_at"])


@transaction.atomic
def set_checklist(
    *, visit: MaintenanceVisit, items: list[ChecklistEntry]
) -> list[MaintenanceVisitItem]:
    """Replace the visit's checklist rows with exactly this set.

    Closing the old rows and opening new ones, the same shape
    apps.contracts.services already uses for renew()/terminate() - not an
    in-place update, so a soft-deleted row still tells the truth about what
    it originally said.
    """
    visit.items.all().delete()
    return [
        MaintenanceVisitItem.objects.create(
            company_id=visit.company_id,
            visit=visit,
            item=entry["item"],
            result=entry["result"],
            note=entry.get("note") or "",
        )
        for entry in items
    ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add apps/maintenance/services.py core/error_codes.py tests/test_maintenance_visits.py
git commit -m "SL-119-feat: recalculate Elevator.next_maintenance_date from completed visits"
```

---

### Task 5: Permission matrix — `maintenance_visit` and `maintenance_visit_edit`

**Files:**
- Modify: `core/permissions.py`
- Test: `tests/test_maintenance_permissions.py` (new file)

**Interfaces:**
- Consumes: `core.permissions.MATRIX`, `core.permissions.may`, `apps.users.models.Role`
- Produces: `MATRIX["maintenance_visit"]`, `MATRIX["maintenance_visit_edit"]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_maintenance_permissions.py`:

```python
"""The permission matrix entries a maintenance visit needs.

A technician may create and read a visit but not edit or delete one -
see the design doc's "Karar bekleyen bulgu" section and
resource_by_action, the same mechanism the QR-label endpoint already uses.
"""

from __future__ import annotations

from apps.users.models import Role
from core.permissions import READ, WRITE, may


class TestMaintenanceVisitPermissions:
    def test_technician_can_read_and_create(self):
        assert may(Role.TECHNICIAN, "maintenance_visit", READ)
        assert may(Role.TECHNICIAN, "maintenance_visit", WRITE)

    def test_technician_cannot_edit_or_delete(self):
        assert not may(Role.TECHNICIAN, "maintenance_visit_edit", WRITE)
        assert not may(Role.TECHNICIAN, "maintenance_visit_edit", READ)

    def test_owner_and_admin_can_edit(self):
        assert may(Role.OWNER, "maintenance_visit_edit", WRITE)
        assert may(Role.ADMIN, "maintenance_visit_edit", WRITE)

    def test_accountant_has_no_access(self):
        assert not may(Role.ACCOUNTANT, "maintenance_visit", READ)
        assert not may(Role.ACCOUNTANT, "maintenance_visit", WRITE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_permissions.py -v`
Expected: FAIL — `may()` returns `False` for
`may(Role.TECHNICIAN, "maintenance_visit", WRITE)` because the key is
missing from `MATRIX` (an empty `set()` default), so the "can create" and
"owner can edit" assertions fail.

- [ ] **Step 3: Add the matrix entries**

In `core/permissions.py`, add to `MATRIX` (after the `"attachment"` entry):

```python
    "maintenance_visit": {
        # A technician records their own visit; Operations can too, on
        # behalf of a technician without an account yet. Accounting has no
        # reason to see this and Operations already covers Owner/Admin's
        # oversight need without adding them here separately.
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    },
    # A visit is not a technician's to correct after the fact - see
    # resource_by_action on MaintenanceVisitViewSet, the same mechanism the
    # QR-label endpoint uses to split "print" from "edit the elevator".
    "maintenance_visit_edit": {
        READ: set(),
        WRITE: {Role.OWNER, Role.ADMIN},
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_permissions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/permissions.py tests/test_maintenance_permissions.py
git commit -m "SL-119-feat: add maintenance_visit to the permission matrix"
```

---

### Task 6: Serializers

**Files:**
- Create: `apps/maintenance/api/__init__.py`, `apps/maintenance/api/v1/__init__.py`, `apps/maintenance/api/v1/serializers.py`
- Test: `tests/test_maintenance_visits.py`

**Interfaces:**
- Consumes: `MaintenanceVisit`, `MaintenanceVisitItem`, `VisitStatus`, `ChecklistItem`, `ChecklistResult` (Task 2)
- Produces: `MaintenanceVisitItemSerializer`, `MaintenanceVisitReadSerializer`, `MaintenanceVisitWriteSerializer`, `SetChecklistSerializer`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maintenance_visits.py`:

```python
class TestMaintenanceVisitSerializers:
    def test_read_serializer_nests_items(self, visit_fixture):
        from apps.maintenance.api.v1.serializers import MaintenanceVisitReadSerializer

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            MaintenanceVisitItem.objects.create(
                company=company, visit=visit, item=ChecklistItem.BRAKE_SYSTEM, result=ChecklistResult.OK
            )
            data = MaintenanceVisitReadSerializer(visit).data

        assert data["status"] == VisitStatus.COMPLETED
        assert len(data["items"]) == 1
        assert data["items"][0]["item"] == ChecklistItem.BRAKE_SYSTEM

    def test_write_serializer_rejects_unknown_fields(self, visit_fixture):
        from apps.maintenance.api.v1.serializers import MaintenanceVisitWriteSerializer

        company, owner, elevator, contract = visit_fixture
        serializer = MaintenanceVisitWriteSerializer(
            data={
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
                "not_a_real_field": "x",
            }
        )
        assert not serializer.is_valid()
        assert "not_a_real_field" in serializer.errors
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: FAIL — `ModuleNotFoundError` for
`apps.maintenance.api.v1.serializers`

- [ ] **Step 3: Write the serializers**

`apps/maintenance/api/__init__.py` and `apps/maintenance/api/v1/__init__.py`:
empty files.

`apps/maintenance/api/v1/serializers.py`:

```python
from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)


class StrictMixin:
    """Rejects a field the client sent that this serializer does not declare.

    Copied rather than imported - every app that needs this defines its own
    (see apps/contracts, apps/customers, apps/elevators, apps/properties);
    there is no shared core.serializers module to pull it from.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


class MaintenanceVisitItemSerializer(serializers.ModelSerializer[MaintenanceVisitItem]):
    class Meta:
        model = MaintenanceVisitItem
        fields = ["id", "item", "result", "note"]
        read_only_fields = ["id"]


class MaintenanceVisitReadSerializer(serializers.ModelSerializer[MaintenanceVisit]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    technician_name = serializers.SerializerMethodField()
    items = MaintenanceVisitItemSerializer(many=True, read_only=True)

    class Meta:
        model = MaintenanceVisit
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "contract",
            "technician",
            "technician_name",
            "visit_date",
            "status",
            "status_note",
            "work_notes",
            "parts_replaced",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_technician_name(self, visit: MaintenanceVisit) -> str:
        user = visit.technician
        return f"{user.first_name} {user.last_name}".strip()


class MaintenanceVisitWriteSerializer(StrictMixin, serializers.ModelSerializer[MaintenanceVisit]):
    status = serializers.ChoiceField(choices=VisitStatus.choices)

    class Meta:
        model = MaintenanceVisit
        fields = [
            "elevator",
            "contract",
            "technician",
            "visit_date",
            "status",
            "status_note",
            "work_notes",
            "parts_replaced",
        ]


class SetChecklistItemSerializer(serializers.Serializer[Any]):
    item = serializers.ChoiceField(choices=ChecklistItem.choices)
    result = serializers.ChoiceField(choices=ChecklistResult.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class SetChecklistSerializer(serializers.Serializer[Any]):
    items = SetChecklistItemSerializer(many=True)

    def validate_items(
        self, items: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        seen = {entry["item"] for entry in items}
        if len(seen) != len(items):
            raise serializers.ValidationError("Each checklist item may appear at most once.")
        return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/maintenance/api tests/test_maintenance_visits.py
git commit -m "SL-119-feat: add maintenance visit serializers"
```

---

### Task 7: ViewSet, `set-checklist` action, URLs

**Files:**
- Create: `apps/maintenance/api/v1/views.py`, `apps/maintenance/api/v1/urls.py`
- Modify: `config/urls.py`
- Test: `tests/test_maintenance_visits.py`

**Interfaces:**
- Consumes: `core.viewsets.TenantViewSet`, `core.permissions.RolePermission`, everything from Tasks 2, 4, 5, 6
- Produces: `MaintenanceVisitViewSet`; routes `maintenance-visits` (`basename="maintenance-visit"`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maintenance_visits.py`:

```python
from django.urls import reverse
from rest_framework.test import APIClient

from apps.users.services import issue_tokens


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


class TestMaintenanceVisitAPI:
    def test_owner_can_create_a_visit_and_set_its_checklist(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
            },
        )
        assert response.status_code == 201, response.data
        visit_id = response.data["id"]

        checklist_response = api_for(owner).post(
            reverse("maintenance-visit-set-checklist", args=[visit_id]),
            {"items": [{"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK}]},
        )
        assert checklist_response.status_code == 200, checklist_response.data
        assert len(checklist_response.data["items"]) == 1

        elevator.refresh_from_db()
        assert elevator.last_maintenance_date == date.today()

    def test_create_without_an_active_contract_is_a_422(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            contract.status = ContractStatus.DRAFT
            contract.save(update_fields=["status"])

        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.COMPLETED,
            },
        )
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.NO_ACTIVE_CONTRACT_FOR_ELEVATOR.value

    def test_incomplete_status_does_not_move_next_maintenance_date(self, visit_fixture):
        company, owner, elevator, contract = visit_fixture
        response = api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": VisitStatus.ACCESS_DENIED,
            },
        )
        assert response.status_code == 201, response.data
        elevator.refresh_from_db()
        assert elevator.next_maintenance_date is None

    def test_technician_cannot_edit_a_visit(self, visit_fixture):
        from apps.users.models import Role
        from apps.users.services import register_company as _rc

        company, owner, elevator, contract = visit_fixture
        with company_context(company.id):
            visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=owner,
                visit_date=date.today(),
                status=VisitStatus.COMPLETED,
            )
            technician = User.objects.create_user(
                company=company,
                email="tech@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=Customer.objects.get(legal_name="Cust")
            )

        response = api_for(technician).patch(
            reverse("maintenance-visit-detail", args=[visit.id]), {"work_notes": "changed"}
        )
        assert response.status_code == 403
```

Add the missing imports at the top of the test file if not already present:
`from apps.contracts.models import ContractStatus` (already imported by
Task 4's block above — reuse it) and `from core.error_codes import ErrorCode`
(same).

`technician.customer_assignments.create(company=..., customer=...)` is the
verified call shape — `core/demo.py:560` does exactly this.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: FAIL — `NoReverseMatch` for `maintenance-visit-list`

- [ ] **Step 3: Write the viewset**

`apps/maintenance/api/v1/views.py`:

```python
from __future__ import annotations

import django_filters
from django.db.models import QuerySet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from apps.maintenance import services
from apps.maintenance.api.v1.serializers import (
    MaintenanceVisitReadSerializer,
    MaintenanceVisitWriteSerializer,
    SetChecklistSerializer,
)
from apps.maintenance.models import MaintenanceVisit
from core.requests import authenticated_user
from core.viewsets import TenantViewSet


class MaintenanceVisitFilter(django_filters.FilterSet):
    customer = django_filters.UUIDFilter(field_name="elevator__customer_id")

    class Meta:
        model = MaintenanceVisit
        fields = ["elevator", "customer", "status", "technician"]


class MaintenanceVisitViewSet(TenantViewSet[MaintenanceVisit]):
    resource = "maintenance_visit"
    resource_by_action = {
        "update": "maintenance_visit_edit",
        "partial_update": "maintenance_visit_edit",
        "destroy": "maintenance_visit_edit",
    }
    read_serializer_class = MaintenanceVisitReadSerializer
    write_serializer_class = MaintenanceVisitWriteSerializer
    filterset_class = MaintenanceVisitFilter
    ordering_fields = ["visit_date", "created_at"]
    ordering = ["-visit_date", "id"]
    customer_path = "elevator__customer_id"

    def get_base_queryset(self) -> QuerySet[MaintenanceVisit]:
        return MaintenanceVisit.objects.select_related(
            "elevator", "contract", "technician"
        ).prefetch_related("items")

    def perform_create(self, serializer: BaseSerializer[MaintenanceVisit]) -> None:
        elevator = serializer.validated_data["elevator"]
        contract = serializer.validated_data["contract"]
        services.ensure_active_contract(elevator=elevator, contract=contract)
        super().perform_create(serializer)
        services.recalculate_next_maintenance(elevator)

    def perform_update(self, serializer: BaseSerializer[MaintenanceVisit]) -> None:
        instance = serializer.instance
        assert instance is not None
        elevator = serializer.validated_data.get("elevator", instance.elevator)
        contract = serializer.validated_data.get("contract", instance.contract)
        services.ensure_active_contract(elevator=elevator, contract=contract)
        super().perform_update(serializer)
        services.recalculate_next_maintenance(elevator)

    def perform_destroy(self, instance: MaintenanceVisit) -> None:
        elevator = instance.elevator
        super().perform_destroy(instance)
        services.recalculate_next_maintenance(elevator)

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        self.perform_create(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)

    def update(self, request: Request, *args: object, **kwargs: object) -> Response:
        instance = self.get_object()
        write = self.get_serializer(instance, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        self.perform_update(write)
        read = self.read_serializer_class(write.instance, context=self.get_serializer_context())
        return Response(read.data)

    @action(detail=True, methods=["post"], url_path="set-checklist")
    def set_checklist(self, request: Request, pk: str | None = None) -> Response:
        """Replace this visit's checklist rows with exactly what is sent.

        A second call, matching how Contract's elevators are managed via
        add-elevators rather than nested inside the contract payload - see
        the plan's file-structure note on why this diverges from the design
        doc's original nested-serializer sketch.
        """
        visit = self.get_object()
        form = SetChecklistSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        services.set_checklist(visit=visit, items=form.validated_data["items"])
        visit.updated_by = authenticated_user(request)
        visit.save(update_fields=["updated_by", "updated_at"])
        read = self.read_serializer_class(visit, context=self.get_serializer_context())
        return Response(read.data)
```

Note: `set_checklist` (the action) is a `WRITE` under the default
`resource = "maintenance_visit"` (technician included) because it is not
listed in `resource_by_action` — intentional, since submitting the
checklist is part of what a technician does when logging their own visit.

`apps/maintenance/api/v1/urls.py`:

```python
from rest_framework.routers import DefaultRouter

from apps.maintenance.api.v1 import views

router = DefaultRouter()
router.register(
    "maintenance-visits", views.MaintenanceVisitViewSet, basename="maintenance-visit"
)

urlpatterns = router.urls
```

In `config/urls.py`, add one line inside the `api/v1/` `include([...])`
list, after `path("", include("apps.attachments.api.v1.urls")),`:

```python
                path("", include("apps.maintenance.api.v1.urls")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_visits.py -v`
Expected: PASS. If `test_technician_cannot_edit_a_visit` fails on the
`customer_assignments.create(...)` call, read `apps/users/models.py` for
the real field names and fix that one line — everything else in the task
does not depend on it.

- [ ] **Step 5: Commit**

```bash
git add apps/maintenance/api/v1/views.py apps/maintenance/api/v1/urls.py config/urls.py tests/test_maintenance_visits.py
git commit -m "SL-119-feat: add the maintenance-visits API"
```

---

### Task 8: Technician can attach a photo to their own visit — nothing else

**Files:**
- Create: `apps/attachments/api/v1/permissions.py`
- Modify: `apps/attachments/services.py`, `apps/attachments/api/v1/views.py`
- Test: `tests/test_maintenance_attachments.py`

**Interfaces:**
- Consumes: `core.permissions.RolePermission`, `apps.attachments.models.ObjectType`, `apps.attachments.models.AttachmentCategory`, `apps.users.models.Role`
- Produces: `AttachmentPermission`; `apps.attachments.services.peek_object_type_and_category(company_id, storage_key) -> tuple[str, str] | tuple[None, None]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_maintenance_attachments.py`:

```python
class TestTechnicianAttachmentCarveOut:
    def test_technician_can_request_an_upload_url_for_a_visit_photo(self, a_visit):
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech2@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(company=company, customer=visit.elevator.building.customer)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.MAINTENANCE_VISIT,
                "object_id": str(visit.id),
                "category": AttachmentCategory.PHOTO,
                "mime_type": "image/jpeg",
                "size_bytes": 1000,
            },
        )
        assert response.status_code == 200, response.data

    def test_technician_cannot_request_an_upload_url_for_a_signed_contract(self, a_visit):
        from apps.users.models import Role
        from apps.users.services import issue_tokens

        company, visit = a_visit
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="tech3@example.com",
                password=PASSWORD,
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(company=company, customer=visit.elevator.building.customer)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(technician).access}")
        response = client.post(
            reverse("attachment-upload-url"),
            {
                "object_type": ObjectType.CONTRACT,
                "object_id": str(visit.contract_id),
                "category": AttachmentCategory.SIGNED_CONTRACT,
                "mime_type": "application/pdf",
                "size_bytes": 1000,
            },
        )
        assert response.status_code == 403


class TestPeekObjectTypeAndCategory:
    def test_reads_back_what_prepare_upload_wrote(self, a_visit):
        from apps.attachments.services import peek_object_type_and_category, prepare_upload

        company, visit = a_visit
        ticket = prepare_upload(
            company_id=company.id,
            object_type=ObjectType.MAINTENANCE_VISIT,
            object_id=visit.id,
            category=AttachmentCategory.PHOTO,
            mime_type="image/jpeg",
            size_bytes=1000,
        )
        object_type, category = peek_object_type_and_category(company.id, ticket.storage_key)
        assert object_type == ObjectType.MAINTENANCE_VISIT
        assert category == AttachmentCategory.PHOTO

    def test_returns_none_none_for_garbage(self, a_visit):
        from apps.attachments.services import peek_object_type_and_category

        company, _ = a_visit
        assert peek_object_type_and_category(company.id, "not-a-real-key") == (None, None)
```

Add `from apps.attachments.models import AttachmentCategory` and
`from apps.users.models import User` and `from rest_framework.test import APIClient`
and `from django.urls import reverse` to the top of
`tests/test_maintenance_attachments.py` if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_maintenance_attachments.py -v`
Expected: FAIL — `ImportError: cannot import name 'peek_object_type_and_category'`,
and the upload-url test gets a 403 where it expects 200 (current matrix has
no `TECHNICIAN` in `attachment` `WRITE`).

- [ ] **Step 3: Add `peek_object_type_and_category`**

In `apps/attachments/services.py`, after `_parse_key`:

```python
def peek_object_type_and_category(
    company_id: uuid.UUID, storage_key: str
) -> tuple[str, str] | tuple[None, None]:
    """What `_parse_key` would say, without raising.

    Used only for a permission check that has to happen before the object is
    confirmed to exist - answering "not allowed" for a malformed or foreign
    key is enough there; `confirm_upload` still does the real, raising parse
    afterwards.
    """
    try:
        object_type, _object_id, category = _parse_key(company_id, storage_key)
    except NotFound:
        return None, None
    return object_type, category
```

- [ ] **Step 4: Write `AttachmentPermission`**

`apps/attachments/api/v1/permissions.py`:

```python
"""One narrow exception on top of the resource matrix.

A technician cannot write any attachment per core.permissions.MATRIX - see
the design doc's "Karar bekleyen bulgu" - but must be able to attach an
optional photo to their own maintenance visit. Every other combination of
object_type/category still falls through to the matrix and is denied.
Widening the matrix entry itself was rejected: it would also open signed
contracts and CE certificates to a technician, which is not the intent.
"""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.views import APIView

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.attachments.services import peek_object_type_and_category
from apps.users.models import Role
from core.permissions import RolePermission
from core.requests import authenticated_company_id


class AttachmentPermission(RolePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        if super().has_permission(request, view):
            return True

        user = request.user
        if not user or not user.is_authenticated or getattr(user, "role", None) != Role.TECHNICIAN:
            return False
        if request.method != "POST":
            return False

        action = getattr(view, "action", None)
        if action == "upload_url":
            return (
                request.data.get("object_type") == ObjectType.MAINTENANCE_VISIT
                and request.data.get("category") == AttachmentCategory.PHOTO
            )
        if action == "create":
            storage_key = request.data.get("storage_key", "")
            object_type, category = peek_object_type_and_category(
                authenticated_company_id(request), storage_key
            )
            return object_type == ObjectType.MAINTENANCE_VISIT and category == AttachmentCategory.PHOTO
        return False
```

In `apps/attachments/api/v1/views.py`, swap the import and class attribute:

```python
from apps.attachments.api.v1.permissions import AttachmentPermission
```

(remove `RolePermission` from the existing import of `core.permissions` if
it is no longer used elsewhere in that file — check first), and change:

```python
    permission_classes = [AttachmentPermission]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_maintenance_attachments.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/attachments/api/v1/permissions.py apps/attachments/services.py apps/attachments/api/v1/views.py tests/test_maintenance_attachments.py
git commit -m "SL-119-feat: let a technician attach a photo to their own maintenance visit"
```

---

### Task 9: Full verification, OpenAPI spec, and syncing the design doc

**Files:**
- Modify: `docs/superpowers/specs/2026-09-12-maintenance-visit-design.md`
- Modify: `openapi/v1.yaml` (generated)

**Interfaces:**
- Consumes: everything from Tasks 1–8

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: every test passes, including the full pre-existing suite (no
regressions). Note any `UnorderedObjectListWarning` — there should be none
for `maintenance-visits`, since `ordering` was set explicitly in Task 7.

- [ ] **Step 2: Lint, type-check, and Django check**

Run: `make lint && make mypy && make check`
Expected: all clean. Fix anything mypy flags on the new files — the
`resource_by_action`/`get_base_queryset`/`perform_*` overrides in Task 7
follow the exact shape of `ElevatorViewSet`/`ComplexViewSet`, so a mypy
complaint there usually means a return type or generic parameter was typed
differently from those two; match them.

- [ ] **Step 3: Regenerate the OpenAPI spec**

Run: `uv run python manage.py spectacular --file openapi/v1.yaml --fail-on-warn`
Expected: succeeds with no warnings. If it warns about an ambiguous type
(commonly a `SerializerMethodField` needing `@extend_schema_field`, as
`ContractReadSerializer.get_monthly_subtotal` already demonstrates), add the
matching decorator to `technician_name` / `elevator_name` in
`MaintenanceVisitReadSerializer` and rerun.

Run: `git diff openapi/v1.yaml` and check the diff only adds
`/api/v1/maintenance-visits/` paths and schemas — nothing else should move.

- [ ] **Step 4: Update the CHANGELOG**

This adds new endpoints (backwards compatible — nothing existing changed
shape). Add a bullet under `## v1 — unreleased` → `### Added` in
`CHANGELOG-API.md`, in the same voice as the existing entries:

```markdown
- **Maintenance visits** — `POST/GET /maintenance-visits/`,
  `GET/PATCH/DELETE /maintenance-visits/{id}/`,
  `POST /maintenance-visits/{id}/set-checklist/`. Records a technician's
  routine maintenance visit against an elevator, with a 16-item checklist,
  and recalculates the elevator's `next_maintenance_date`. A technician may
  create their own visit and attach an optional photo to it, but only
  `OWNER`/`ADMIN` may edit or delete one afterwards.
```

- [ ] **Step 5: Sync the design doc to the actual `set-checklist` action**

In `docs/superpowers/specs/2026-09-12-maintenance-visit-design.md`, replace
the paragraph under "`items` (the checklist rows) is a **writable nested
serializer**..." with:

```markdown
`items` (the checklist rows) are set via a separate action —
`POST /maintenance-visits/{id}/set-checklist/`, replacing the full set in
one call — the same shape `Contract`/`ContractElevator` already uses via
`add-elevators`, rather than a writable nested serializer on
`MaintenanceVisit` itself. See the implementation plan
(`docs/superpowers/plans/2026-09-12-maintenance-visit.md`) for why this
changed during planning.
```

- [ ] **Step 6: Final full verification and commit**

Run: `make lint && make test && make check && make mypy`
Expected: all green.

```bash
git add openapi/v1.yaml CHANGELOG-API.md docs/superpowers/specs/2026-09-12-maintenance-visit-design.md
git commit -m "SL-119-docs: regenerate the API contract and sync the design doc"
```

---

## Self-Review Notes

- **Spec coverage:** every acceptance-criterion bullet in the design doc's
  "Testing plan" section has a matching test in Tasks 1, 4, 7, or 8. The
  "active contract... as of `visit_date`" wording in the spec is
  implemented as "contract is currently `ACTIVE` and currently covers the
  elevator" (Task 4) rather than a temporal range check — consistent with
  how `ContractStatus.ACTIVE` is treated as a present-tense gate everywhere
  else in this codebase (`apps/contracts/services.py` never checks a date
  range against `status`, only against `start_date`/`end_date` directly on
  the contract itself, which this feature does not touch).
- **Nested-serializer deviation:** called out at the top of this document
  and re-synced into the spec in Task 9, so both documents agree once this
  plan is done.
- **Type consistency:** `ChecklistEntry` (Task 4) is a `TypedDict` with
  `item`/`result`/`note`; `SetChecklistItemSerializer.validated_data`
  (Task 6) produces dicts with exactly those keys, which is what
  `services.set_checklist` (Task 4) and the `set_checklist` action (Task 7)
  both consume — verified by hand across the three tasks.
