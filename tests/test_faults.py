"""Faults, for the firm's own people — M4, slice 1.

docs/superpowers/specs/2026-10-20-m4-fault-management-design.md. In the order
a fault lives: reported, sent, arrived at, resolved (and billed or not),
closed — with the wrong-state refusals, who may press which button, who hears
about it, the photos, and the retention job for public reporters.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.audit.models import AuditLog
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator, ElevatorStatus
from apps.elevators.services import assign_qr_token
from apps.faults.models import (
    REFERENCE_ALPHABET,
    Fault,
    FaultChannel,
    FaultPriority,
    FaultStatus,
)
from apps.faults.tasks import purge_public_reporter_data
from apps.notifications.models import Notification, NotificationKind
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core import scheduling
from core.context import company_context, system_context
from core.error_codes import ErrorCode
from tests.test_attachments import FakeS3, uploaded

PASSWORD = "correct-horse-battery"
TODAY = date.today()


def make_user(company: Any, role: str, email: str, first_name: str = "") -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name=first_name or role.title(),
            last_name="X",
            role=role,
            is_email_verified=True,
        )


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


class Shop:
    """One firm: a customer with one lift under a maintenance-only contract.

    - `tech` and `tech2` are both assigned to the customer; `outsider` is
      assigned to another customer of the same firm.
    """

    def __init__(self, letter: str = "") -> None:
        suffix = letter.lower()
        self.company, self.owner = register_company(
            legal_name=f"Shop {letter} Ltd",
            display_name=f"Shop {letter}",
            first_name="Nur",
            last_name="Owner",
            email=f"owner{suffix}@example.com",
            password=PASSWORD,
        )
        self.admin = make_user(self.company, Role.ADMIN, f"admin{suffix}@example.com")
        self.ops = make_user(self.company, Role.OPERATIONS, f"ops{suffix}@example.com")
        self.accountant = make_user(self.company, Role.ACCOUNTANT, f"acc{suffix}@example.com")
        self.tech = make_user(self.company, Role.TECHNICIAN, f"tech{suffix}@example.com", "Ali")
        self.tech2 = make_user(self.company, Role.TECHNICIAN, f"tech2{suffix}@example.com", "Veli")
        self.outsider = make_user(
            self.company, Role.TECHNICIAN, f"outsider{suffix}@example.com", "Can"
        )
        with company_context(self.company.id):
            self.customer = Customer.objects.create(
                company=self.company, type=CustomerType.CORPORATE, legal_name=f"Site {letter}"
            )
            other = Customer.objects.create(
                company=self.company, type=CustomerType.CORPORATE, legal_name=f"Other {letter}"
            )
            self.building = Building.objects.create(
                company=self.company,
                customer=self.customer,
                name=f"A Blok {letter}",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
            )
            self.elevator = Elevator(
                company=self.company, building=self.building, name="Sol", registration_number="1"
            )
            assign_qr_token(self.elevator)
            self.elevator.status = ElevatorStatus.ACTIVE
            self.elevator.save()
            for tech in (self.tech, self.tech2):
                tech.customer_assignments.create(company=self.company, customer=self.customer)
            self.outsider.customer_assignments.create(company=self.company, customer=other)
            self.contract = Contract.objects.create(
                company=self.company,
                customer=self.customer,
                contract_number=f"C-{letter}1",
                status=ContractStatus.ACTIVE,
                scope=Scope.MAINTENANCE_ONLY,
                start_date=TODAY - timedelta(days=100),
                end_date=TODAY + timedelta(days=265),
                pricing_type=PricingType.FLAT,
                monthly_fee="1000.00",
                vat_rate="20.00",
            )
            self.line = ContractElevator.objects.create(
                company=self.company,
                contract=self.contract,
                elevator=self.elevator,
                added_at=self.contract.start_date,
            )

    def report(self, by: User | None = None, **body: Any) -> dict[str, Any]:
        response = api_for(by or self.owner).post(
            reverse("fault-list"),
            {"elevator": str(self.elevator.id), "description": "Kapı kapanmıyor", **body},
            format="json",
        )
        assert response.status_code == 201, response.data
        return dict(response.data)

    def act(self, fault_id: Any, verb: str, by: User | None = None, **body: Any) -> Any:
        return api_for(by or self.owner).post(
            reverse(f"fault-{verb}", args=[fault_id]), body, format="json"
        )

    def fault(self, fault_id: Any) -> Fault:
        with company_context(self.company.id):
            return Fault.objects.get(pk=fault_id)

    def lift_status(self) -> str:
        return Elevator.unscoped.get(pk=self.elevator.pk).status

    def to_on_site(self, **body: Any) -> str:
        fault_id = self.report(**body)["id"]
        assert self.act(fault_id, "assign", technician=str(self.tech.id)).status_code == 200
        assert self.act(fault_id, "arrive", by=self.tech).status_code == 200
        return str(fault_id)


@pytest.fixture
def shop(db) -> Shop:
    return Shop()


def told(kind: str) -> set[str]:
    return {
        n.recipient.email
        for n in Notification.unscoped.filter(kind=kind).select_related("recipient")
    }


def error_code(response: Any) -> str:
    return str(response.data["error"]["code"])


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestReporting:
    def test_a_staff_report_opens_a_fault(self, shop):
        body = shop.report()
        assert body["status"] == FaultStatus.OPEN
        assert body["channel"] == FaultChannel.STAFF
        assert body["priority"] == FaultPriority.NORMAL
        assert body["reported_by"] == shop.owner.id
        assert body["reported_by_name"] == "Nur Owner"
        assert len(body["reference"]) == 6
        assert set(body["reference"]) <= set(REFERENCE_ALPHABET)
        assert body["reported_at"] is not None
        for stamp in ("assigned_at", "on_site_at", "resolved_at", "closed_at", "cancelled_at"):
            assert body[stamp] is None
        assert body["billable"] is None
        assert body["response_time_seconds"] is None

    def test_with_a_technician_it_starts_assigned(self, shop):
        body = shop.report(technician=str(shop.tech.id))
        assert body["status"] == FaultStatus.ASSIGNED
        assert body["assigned_at"] == body["reported_at"]
        assert told(NotificationKind.FAULT_ASSIGNED) == {shop.tech.email}

    def test_the_office_and_the_customers_technicians_are_told(self, shop):
        shop.report(by=shop.ops)
        # Not the outsider (another customer), not the reporter themselves.
        assert told(NotificationKind.FAULT_REPORTED) == {
            shop.owner.email,
            shop.admin.email,
            shop.tech.email,
            shop.tech2.email,
        }
        assert told(NotificationKind.FAULT_EMERGENCY) == set()

    def test_a_trapped_passenger_is_an_emergency_whatever_was_sent(self, shop):
        body = shop.report(priority=FaultPriority.NORMAL, someone_trapped=True)
        assert body["priority"] == FaultPriority.EMERGENCY
        assert told(NotificationKind.FAULT_EMERGENCY) == {
            shop.admin.email,
            shop.ops.email,
            shop.tech.email,
            shop.tech2.email,
        }
        # One or the other, never both.
        assert told(NotificationKind.FAULT_REPORTED) == set()

    def test_an_emergency_also_reaches_the_assigned_technician(self, shop):
        shop.report(someone_trapped=True, technician=str(shop.outsider.id))
        assert shop.outsider.email in told(NotificationKind.FAULT_EMERGENCY)

    def test_a_stopped_lift_goes_out_of_service(self, shop):
        body = shop.report(stopped_elevator=True)
        assert body["stopped_elevator"] is True
        assert shop.lift_status() == ElevatorStatus.OUT_OF_SERVICE

    def test_only_an_active_lift_is_put_out_of_service(self, shop):
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(status=ElevatorStatus.SEALED)
        shop.report(stopped_elevator=True)
        assert shop.lift_status() == ElevatorStatus.SEALED

    def test_state_and_timestamps_are_not_the_clients(self, shop):
        response = api_for(shop.owner).post(
            reverse("fault-list"),
            {
                "elevator": str(shop.elevator.id),
                "description": "x",
                "status": FaultStatus.RESOLVED,
                "on_site_at": "2026-01-01T00:00:00Z",
            },
            format="json",
        )
        assert response.status_code == 400
        assert not Fault.unscoped.exists()

    def test_the_description_is_required_and_bounded(self, shop):
        client = api_for(shop.owner)
        url = reverse("fault-list")
        assert client.post(url, {"elevator": str(shop.elevator.id)}).status_code == 400
        too_long = {"elevator": str(shop.elevator.id), "description": "x" * 1001}
        assert client.post(url, too_long, format="json").status_code == 400

    def test_a_technician_reports_on_their_customers_lifts(self, shop):
        body = shop.report(by=shop.tech, technician=str(shop.tech.id))
        assert body["status"] == FaultStatus.ASSIGNED
        # They assigned it to themselves; nobody needs to tell them.
        assert told(NotificationKind.FAULT_ASSIGNED) == set()

    def test_a_technician_cannot_report_on_another_customers_lift(self, shop):
        response = api_for(shop.outsider).post(
            reverse("fault-list"),
            {"elevator": str(shop.elevator.id), "description": "x"},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "elevator", "code": "does_not_exist"}
        ]

    def test_a_technician_cannot_send_somebody_else(self, shop):
        response = api_for(shop.tech).post(
            reverse("fault-list"),
            {
                "elevator": str(shop.elevator.id),
                "description": "x",
                "technician": str(shop.tech2.id),
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_user_of_another_firm_cannot_be_sent(self, shop):
        other = Shop("B")
        response = api_for(shop.owner).post(
            reverse("fault-list"),
            {
                "elevator": str(shop.elevator.id),
                "description": "x",
                "technician": str(other.tech.id),
            },
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "technician", "code": "does_not_exist"}
        ]

    def test_the_accountant_has_no_access(self, shop):
        client = api_for(shop.accountant)
        assert client.get(reverse("fault-list")).status_code == 403
        response = client.post(
            reverse("fault-list"), {"elevator": str(shop.elevator.id), "description": "x"}
        )
        assert response.status_code == 403

    def test_there_is_no_delete(self, shop):
        fault_id = shop.report()["id"]
        response = api_for(shop.owner).delete(reverse("fault-detail", args=[fault_id]))
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTheHappyPath:
    def test_reported_assigned_on_site_resolved_closed(self, shop):
        fault_id = shop.report()["id"]

        assigned = shop.act(fault_id, "assign", technician=str(shop.tech.id))
        assert assigned.status_code == 200
        assert assigned.data["status"] == FaultStatus.ASSIGNED
        assert assigned.data["technician_name"] == "Ali X"

        on_site = shop.act(fault_id, "arrive", by=shop.tech)
        assert on_site.status_code == 200
        assert on_site.data["status"] == FaultStatus.ON_SITE
        assert on_site.data["response_time_seconds"] is not None

        resolved = shop.act(
            fault_id, "resolve", by=shop.tech, resolution_notes="Kapı kilidi", parts_used=""
        )
        assert resolved.status_code == 200
        assert resolved.data["status"] == FaultStatus.RESOLVED
        assert resolved.data["resolution_notes"] == "Kapı kilidi"

        closed = shop.act(fault_id, "close")
        assert closed.status_code == 200
        assert closed.data["status"] == FaultStatus.CLOSED

        fault = shop.fault(fault_id)
        assert fault.reported_at <= fault.assigned_at <= fault.on_site_at
        assert fault.on_site_at <= fault.resolved_at <= fault.closed_at

    def test_response_and_resolution_times_are_derived(self, shop):
        fault_id = shop.report()["id"]
        start = timezone.now() - timedelta(hours=2)
        Fault.unscoped.filter(pk=fault_id).update(
            reported_at=start,
            on_site_at=start + timedelta(minutes=15),
            resolved_at=start + timedelta(minutes=75),
        )
        body = api_for(shop.owner).get(reverse("fault-detail", args=[fault_id])).data
        assert body["response_time_seconds"] == 15 * 60
        assert body["resolution_time_seconds"] == 75 * 60

    def test_resolving_tells_the_office(self, shop):
        fault_id = shop.to_on_site()
        shop.act(fault_id, "resolve", by=shop.tech)
        assert told(NotificationKind.FAULT_RESOLVED) == {
            shop.owner.email,
            shop.admin.email,
            shop.ops.email,
        }
        (notification,) = Notification.unscoped.filter(kind=NotificationKind.FAULT_RESOLVED)[:1]
        assert notification.data["technician_name"] == "Ali X"

    def test_reassigning_sends_somebody_else_and_tells_them(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        first = shop.fault(fault_id).assigned_at
        response = shop.act(fault_id, "assign", technician=str(shop.tech2.id))
        assert response.status_code == 200
        fault = shop.fault(fault_id)
        assert fault.technician_id == shop.tech2.id
        assert fault.assigned_at > first
        assert told(NotificationKind.FAULT_ASSIGNED) == {shop.tech.email, shop.tech2.email}

    def test_assigning_the_same_technician_again_changes_nothing(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        first = shop.fault(fault_id).assigned_at
        shop.act(fault_id, "assign", technician=str(shop.tech.id))
        assert shop.fault(fault_id).assigned_at == first
        assert Notification.unscoped.filter(kind=NotificationKind.FAULT_ASSIGNED).count() == 1

    def test_restore_service_puts_the_lift_back(self, shop):
        fault_id = shop.to_on_site(stopped_elevator=True)
        assert shop.lift_status() == ElevatorStatus.OUT_OF_SERVICE
        shop.act(fault_id, "resolve", by=shop.tech, restore_service=True)
        assert shop.lift_status() == ElevatorStatus.ACTIVE

    def test_without_restore_service_the_lift_stays_out(self, shop):
        fault_id = shop.to_on_site(stopped_elevator=True)
        shop.act(fault_id, "resolve", by=shop.tech)
        assert shop.lift_status() == ElevatorStatus.OUT_OF_SERVICE

    def test_cancel_needs_a_reason(self, shop):
        fault_id = shop.report()["id"]
        assert shop.act(fault_id, "cancel").status_code == 400
        response = shop.act(fault_id, "cancel", reason="Mükerrer bildirim")
        assert response.status_code == 200
        assert response.data["status"] == FaultStatus.CANCELLED
        assert response.data["cancel_reason"] == "Mükerrer bildirim"
        assert response.data["cancelled_at"] is not None

    @pytest.mark.parametrize("stage", ["open", "assigned", "on_site"])
    def test_cancel_from_any_state_before_resolved(self, shop, stage):
        fault_id = shop.report()["id"]
        if stage in ("assigned", "on_site"):
            shop.act(fault_id, "assign", technician=str(shop.tech.id))
        if stage == "on_site":
            shop.act(fault_id, "arrive", by=shop.tech)
        assert shop.act(fault_id, "cancel", reason="x").status_code == 200


def at_stage(shop: Shop, stage: str) -> str:
    """A fault brought to `stage` through the API."""
    if stage == FaultStatus.OPEN:
        return str(shop.report()["id"])
    if stage == FaultStatus.ASSIGNED:
        return str(shop.report(technician=str(shop.tech.id))["id"])
    fault_id = shop.to_on_site()
    if stage == FaultStatus.ON_SITE:
        return fault_id
    shop.act(fault_id, "resolve", by=shop.tech)
    if stage == FaultStatus.RESOLVED:
        return fault_id
    if stage == FaultStatus.CLOSED:
        shop.act(fault_id, "close")
        return fault_id
    raise AssertionError(stage)


def cancelled(shop: Shop) -> str:
    fault_id = str(shop.report()["id"])
    shop.act(fault_id, "cancel", reason="x")
    return fault_id


class TestTheWrongState:
    @pytest.mark.parametrize(
        ("stage", "verb", "body"),
        [
            (FaultStatus.OPEN, "arrive", {}),
            (FaultStatus.OPEN, "resolve", {}),
            (FaultStatus.OPEN, "close", {}),
            (FaultStatus.ASSIGNED, "resolve", {}),
            (FaultStatus.ASSIGNED, "close", {}),
            (FaultStatus.ON_SITE, "arrive", {}),
            (FaultStatus.ON_SITE, "assign", {"technician": "TECH"}),
            (FaultStatus.ON_SITE, "close", {}),
            (FaultStatus.RESOLVED, "assign", {"technician": "TECH"}),
            (FaultStatus.RESOLVED, "arrive", {}),
            (FaultStatus.RESOLVED, "resolve", {}),
            (FaultStatus.RESOLVED, "cancel", {"reason": "x"}),
            (FaultStatus.CLOSED, "close", {}),
            (FaultStatus.CLOSED, "cancel", {"reason": "x"}),
        ],
    )
    def test_is_refused_with_409(self, shop, stage, verb, body):
        fault_id = at_stage(shop, stage)
        before = shop.fault(fault_id)
        body = {k: (str(shop.tech2.id) if v == "TECH" else v) for k, v in body.items()}

        response = shop.act(fault_id, verb, **body)
        assert response.status_code == 409, response.data
        assert error_code(response) == ErrorCode.FAULT_TRANSITION_NOT_ALLOWED.value
        after = shop.fault(fault_id)
        assert (after.status, after.technician_id, after.updated_at) == (
            before.status,
            before.technician_id,
            before.updated_at,
        )

    @pytest.mark.parametrize("verb", ["assign", "arrive", "resolve", "close", "cancel"])
    def test_a_cancelled_fault_takes_no_action(self, shop, verb):
        fault_id = cancelled(shop)
        body = {"technician": str(shop.tech.id)} if verb == "assign" else {"reason": "x"}
        response = shop.act(fault_id, verb, **body)
        assert response.status_code == 409
        assert error_code(response) == ErrorCode.FAULT_TRANSITION_NOT_ALLOWED.value


class TestWhoMayDoWhat:
    def test_only_the_assigned_technician_works_the_fault(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        # tech2 sees the fault (same customer) but was not sent to it.
        assert shop.act(fault_id, "arrive", by=shop.tech2).status_code == 403
        assert shop.act(fault_id, "arrive", by=shop.tech).status_code == 200
        assert shop.act(fault_id, "resolve", by=shop.tech2).status_code == 403
        assert shop.act(fault_id, "resolve", by=shop.tech).status_code == 200

    def test_the_office_may_record_either_on_their_behalf(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        assert shop.act(fault_id, "arrive", by=shop.ops).status_code == 200
        assert shop.act(fault_id, "resolve", by=shop.admin).status_code == 200

    @pytest.mark.parametrize("verb", ["assign", "close", "cancel"])
    def test_a_technician_cannot_take_the_offices_actions(self, shop, verb):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        body = {"technician": str(shop.tech.id), "reason": "x"}
        assert shop.act(fault_id, verb, by=shop.tech, **body).status_code == 403

    def test_a_technician_cannot_patch(self, shop):
        fault_id = shop.report()["id"]
        response = api_for(shop.tech).patch(
            reverse("fault-detail", args=[fault_id]), {"priority": "high"}, format="json"
        )
        assert response.status_code == 403

    def test_a_technician_sees_only_their_customers_faults(self, shop):
        fault_id = shop.report()["id"]
        rows = api_for(shop.tech).get(reverse("fault-list")).data["results"]
        assert [row["id"] for row in rows] == [fault_id]
        assert api_for(shop.outsider).get(reverse("fault-list")).data["results"] == []
        detail = api_for(shop.outsider).get(reverse("fault-detail", args=[fault_id]))
        assert detail.status_code == 404


# ---------------------------------------------------------------------------
# Billable
# ---------------------------------------------------------------------------


class TestBillable:
    @pytest.mark.parametrize(
        ("scope", "parts", "billable"),
        [
            (Scope.MAINTENANCE_ONLY, "", True),
            (Scope.MAINTENANCE_ONLY, "Kilit", True),
            (Scope.MAINTENANCE_AND_REPAIR, "", False),
            (Scope.MAINTENANCE_AND_REPAIR, "Kapı kilidi x1", True),
            (Scope.FULL_COVERAGE, "Motor", False),
        ],
    )
    def test_by_the_contracts_scope(self, shop, scope, parts, billable):
        Contract.unscoped.filter(pk=shop.contract.pk).update(scope=scope)
        fault_id = shop.to_on_site()
        response = shop.act(fault_id, "resolve", by=shop.tech, parts_used=parts)
        assert response.data["billable"] is billable
        assert response.data["billable_overridden"] is False

    def test_no_contract_is_billable(self, shop):
        Contract.unscoped.filter(pk=shop.contract.pk).update(scope=Scope.FULL_COVERAGE)
        ContractElevator.unscoped.filter(pk=shop.line.pk).update(
            removed_at=TODAY - timedelta(days=10)
        )
        fault_id = shop.to_on_site()
        assert shop.act(fault_id, "resolve", by=shop.tech).data["billable"] is True

    def test_a_draft_contract_covers_nothing(self, shop):
        Contract.unscoped.filter(pk=shop.contract.pk).update(
            scope=Scope.FULL_COVERAGE, status=ContractStatus.DRAFT
        )
        fault_id = shop.to_on_site()
        assert shop.act(fault_id, "resolve", by=shop.tech).data["billable"] is True

    def test_it_is_the_contract_on_the_day_of_the_report(self, shop):
        # Full coverage when the fault was reported; terminated since.
        Contract.unscoped.filter(pk=shop.contract.pk).update(
            scope=Scope.FULL_COVERAGE, status=ContractStatus.TERMINATED, terminated_at=TODAY
        )
        ContractElevator.unscoped.filter(pk=shop.line.pk).update(removed_at=TODAY)
        fault_id = shop.to_on_site()
        Fault.unscoped.filter(pk=fault_id).update(reported_at=timezone.now() - timedelta(days=1))
        assert shop.act(fault_id, "resolve", by=shop.tech).data["billable"] is False

    def test_the_office_overrides_it_and_resolve_respects_that(self, shop):
        fault_id = shop.to_on_site()
        response = api_for(shop.owner).patch(
            reverse("fault-detail", args=[fault_id]), {"billable": False}, format="json"
        )
        assert response.status_code == 200
        assert response.data["billable"] is False
        assert response.data["billable_overridden"] is True

        resolved = shop.act(fault_id, "resolve", by=shop.tech)
        # Maintenance only would derive "yes"; the office said no.
        assert resolved.data["billable"] is False

    def test_an_override_after_resolve_is_recorded_and_audited(self, shop):
        fault_id = shop.to_on_site()
        shop.act(fault_id, "resolve", by=shop.tech)
        api_for(shop.ops).patch(
            reverse("fault-detail", args=[fault_id]), {"billable": False}, format="json"
        )
        fault = shop.fault(fault_id)
        assert (fault.billable, fault.billable_overridden) == (False, True)
        changes = AuditLog.objects.filter(table_name="fault", record_id=fault.pk, action="update")
        assert any((entry.new_values or {}).get("billable_overridden") is True for entry in changes)


class TestEditing:
    def test_the_office_corrects_priority_and_description(self, shop):
        fault_id = shop.report()["id"]
        response = api_for(shop.ops).patch(
            reverse("fault-detail", args=[fault_id]),
            {"priority": FaultPriority.HIGH, "description": "Kabin sarsılıyor"},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["priority"] == FaultPriority.HIGH
        assert response.data["description"] == "Kabin sarsılıyor"

    def test_raising_it_to_emergency_tells_people(self, shop):
        fault_id = shop.report()["id"]
        api_for(shop.owner).patch(
            reverse("fault-detail", args=[fault_id]),
            {"priority": FaultPriority.EMERGENCY},
            format="json",
        )
        assert shop.tech.email in told(NotificationKind.FAULT_EMERGENCY)

    def test_the_state_is_not_patchable(self, shop):
        fault_id = shop.report()["id"]
        response = api_for(shop.owner).patch(
            reverse("fault-detail", args=[fault_id]),
            {"status": FaultStatus.CLOSED},
            format="json",
        )
        assert response.status_code == 400
        assert shop.fault(fault_id).status == FaultStatus.OPEN


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


class TestList:
    def test_filters_and_the_board_order(self, shop):
        normal = shop.report()["id"]
        emergency = shop.report(someone_trapped=True)["id"]
        high = shop.report(priority=FaultPriority.HIGH)["id"]
        client = api_for(shop.ops)

        board = client.get(reverse("fault-list"), {"ordering": "priority_rank,reported_at"})
        assert [row["id"] for row in board.data["results"]] == [emergency, high, normal]
        only_high = client.get(reverse("fault-list"), {"priority": FaultPriority.HIGH})
        assert [row["id"] for row in only_high.data["results"]] == [high]
        by_customer = client.get(reverse("fault-list"), {"customer": str(shop.customer.id)})
        assert len(by_customer.data["results"]) == 3

    def test_the_query_count_does_not_grow_with_the_rows(self, shop):
        client = api_for(shop.owner)
        shop.report(technician=str(shop.tech.id))
        with CaptureQueriesContext(connection) as one:
            client.get(reverse("fault-list"))
        for _ in range(4):
            shop.report(technician=str(shop.tech2.id))
        with CaptureQueriesContext(connection) as five:
            response = client.get(reverse("fault-list"))
        assert len(response.data["results"]) == 5
        assert len(five) == len(one)

    def test_another_firms_faults_are_out_of_reach(self, shop):
        other = Shop("B")
        theirs = other.report()["id"]
        client = api_for(shop.owner)
        assert client.get(reverse("fault-list")).data["results"] == []
        response = client.post(
            reverse("fault-assign", args=[theirs]), {"technician": str(shop.tech.id)}
        )
        assert response.status_code == 404
        assert other.fault(theirs).status == FaultStatus.OPEN


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


@pytest.fixture
def bucket(monkeypatch) -> FakeS3:
    from core import storage

    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda backend: fake)
    return fake


def ask_upload(user: User, fault_id: Any, category: str = AttachmentCategory.PHOTO) -> Any:
    return api_for(user).post(
        reverse("attachment-upload-url"),
        {
            "object_type": ObjectType.FAULT,
            "object_id": str(fault_id),
            "category": category,
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        },
        format="json",
    )


class TestFaultPhotos:
    def test_the_assigned_technician_photographs_the_fault(self, shop, bucket):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        ticket = ask_upload(shop.tech, fault_id)
        assert ticket.status_code == 200, ticket.data
        uploaded(bucket, ticket.data["storage_key"])
        confirmed = api_for(shop.tech).post(
            reverse("attachment-list"),
            {"storage_key": ticket.data["storage_key"], "original_filename": "kapi.jpg"},
            format="json",
        )
        assert confirmed.status_code == 201, confirmed.data
        listed = api_for(shop.tech).get(
            reverse("attachment-list"), {"object_type": ObjectType.FAULT}
        )
        assert [row["id"] for row in listed.data["results"]] == [confirmed.data["id"]]

    def test_a_technician_not_sent_to_it_cannot(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        # Same customer, so they can see the fault — but it is not theirs.
        assert ask_upload(shop.tech2, fault_id).status_code == 403

    def test_only_photos(self, shop):
        fault_id = shop.report(technician=str(shop.tech.id))["id"]
        assert ask_upload(shop.tech, fault_id, AttachmentCategory.PERMIT).status_code == 403

    def test_the_office_attaches_anything(self, shop):
        fault_id = shop.report()["id"]
        assert ask_upload(shop.ops, fault_id).status_code == 200


# ---------------------------------------------------------------------------
# Retention of public reporters' data
# ---------------------------------------------------------------------------


def public_fault(shop: Shop, *, status: str, days_ago: int, **extra: Any) -> Fault:
    finished = timezone.now() - timedelta(days=days_ago)
    stamps = {"closed_at": finished} if status == FaultStatus.CLOSED else {}
    if status == FaultStatus.CANCELLED:
        stamps = {"cancelled_at": finished}
    with company_context(shop.company.id):
        return Fault.objects.create(
            company=shop.company,
            elevator=shop.elevator,
            reference=f"R{Fault.unscoped.count():05d}",
            channel=extra.pop("channel", FaultChannel.PUBLIC_QR),
            reporter_name="Ayşe Komşu",
            reporter_phone="05321234567",
            description="Asansör durdu",
            status=status,
            **stamps,
            **extra,
        )


class TestRetention:
    def test_names_and_phones_go_90_days_after_the_fault_ends(self, shop):
        old_closed = public_fault(shop, status=FaultStatus.CLOSED, days_ago=91)
        old_cancelled = public_fault(shop, status=FaultStatus.CANCELLED, days_ago=91)
        recent = public_fault(shop, status=FaultStatus.CLOSED, days_ago=89)
        still_open = public_fault(shop, status=FaultStatus.OPEN, days_ago=200)

        assert purge_public_reporter_data.call() == 2

        def contact(fault: Fault) -> tuple[str, str]:
            row = Fault.unscoped.get(pk=fault.pk)
            return row.reporter_name, row.reporter_phone

        assert contact(old_closed) == ("", "")
        assert contact(old_cancelled) == ("", "")
        assert contact(recent) == ("Ayşe Komşu", "05321234567")
        assert contact(still_open) == ("Ayşe Komşu", "05321234567")
        # The fault itself stays: only the personal data goes.
        assert Fault.unscoped.get(pk=old_closed.pk).description == "Asansör durdu"

    def test_a_second_run_finds_nothing(self, shop):
        public_fault(shop, status=FaultStatus.CLOSED, days_ago=120)
        purge_public_reporter_data.call()
        assert purge_public_reporter_data.call() == 0

    def test_it_runs_across_firms(self, shop):
        other = Shop("B")
        public_fault(shop, status=FaultStatus.CLOSED, days_ago=120)
        public_fault(other, status=FaultStatus.CLOSED, days_ago=120)
        assert purge_public_reporter_data.call() == 2

    def test_the_audit_trail_never_held_them(self, shop):
        fault = public_fault(shop, status=FaultStatus.OPEN, days_ago=0)
        entry = AuditLog.objects.get(table_name="fault", record_id=fault.pk, action="create")
        assert entry.new_values is not None
        assert entry.new_values["reporter_name"] == "***"
        assert entry.new_values["reporter_phone"] == "***"

    def test_it_is_scheduled_daily(self):
        assert "purge_public_reporter_data" in {s.name for s in scheduling.get_schedules()}
