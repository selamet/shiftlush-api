"""The monthly maintenance plan — M3, slice 1.

docs/superpowers/specs/2026-10-20-m3-maintenance-planning-design.md. In the
order a row lives: the daily job creates it, a recorded visit closes it, and
the office and the technician read and arrange it through the API. "Today" is
pinned to 2026-10-20, so the plan months are October and November 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest
from django.core import mail as django_mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.address.models import District, Neighborhood, Province
from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.elevators.services import assign_qr_token
from apps.maintenance.models import (
    MaintenanceVisit,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.maintenance.services import due_date_in, period_of
from apps.maintenance.tasks import plan_maintenance
from apps.notifications.models import Notification, NotificationKind
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core import scheduling
from core.context import company_context, system_context
from core.error_codes import ErrorCode
from core.templatetags.email_format import tr_month

PASSWORD = "correct-horse-battery"
TODAY = date(2026, 10, 20)
OCT, NOV = "2026-10", "2026-11"


@pytest.fixture(autouse=True)
def today() -> Iterator[date]:
    with (
        mock.patch("apps.maintenance.tasks.local_today", return_value=TODAY),
        mock.patch("apps.maintenance.api.v1.views.local_today", return_value=TODAY),
        mock.patch("apps.maintenance.api.v1.serializers.local_today", return_value=TODAY),
        mock.patch("apps.maintenance.services.local_today", return_value=TODAY),
    ):
        yield TODAY


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


def make_elevator(company: Any, building: Building, name: str) -> Elevator:
    elevator = Elevator(company=company, building=building, name=name, registration_number=name)
    assign_qr_token(elevator)
    elevator.save()
    return elevator


class Plan:
    """One firm with two customers in two districts, and a contract over three lifts.

    - Customer A (Kadikoy): lifts A1 and A2, exactly one technician (tech_a).
    - Customer B (Beşiktaş): lift B1, two technicians — so no default.
    - Lift X1 belongs to customer A and has no contract.
    """

    def __init__(self, letter: str = "") -> None:
        suffix = letter.lower()
        self.company, self.owner = register_company(
            legal_name=f"Plan {letter} Ltd",
            display_name=f"Plan {letter}",
            first_name="Nur",
            last_name="Owner",
            email=f"owner{suffix}@example.com",
            password=PASSWORD,
        )
        province, _ = Province.objects.get_or_create(
            id=34, defaults={"name": "İstanbul", "name_normalized": "istanbul"}
        )
        kadikoy, _ = District.objects.get_or_create(
            id=3401,
            defaults={"province": province, "name": "Kadıköy", "name_normalized": "kadikoy"},
        )
        besiktas, _ = District.objects.get_or_create(
            id=3402,
            defaults={"province": province, "name": "Beşiktaş", "name_normalized": "besiktas"},
        )
        self.kadikoy, self.besiktas = kadikoy, besiktas
        moda, _ = Neighborhood.objects.get_or_create(
            id=340101, defaults={"district": kadikoy, "name": "Moda", "name_normalized": "moda"}
        )
        levent, _ = Neighborhood.objects.get_or_create(
            id=340201,
            defaults={"district": besiktas, "name": "Levent", "name_normalized": "levent"},
        )

        self.ops = make_user(self.company, Role.OPERATIONS, f"ops{suffix}@example.com")
        self.accountant = make_user(self.company, Role.ACCOUNTANT, f"acc{suffix}@example.com")
        self.tech_a = make_user(self.company, Role.TECHNICIAN, f"tech-a{suffix}@example.com", "Ali")
        self.tech_b1 = make_user(
            self.company, Role.TECHNICIAN, f"tech-b1{suffix}@example.com", "Banu"
        )
        self.tech_b2 = make_user(
            self.company, Role.TECHNICIAN, f"tech-b2{suffix}@example.com", "Can"
        )

        with company_context(self.company.id):
            self.customer_a = Customer.objects.create(
                company=self.company, type=CustomerType.CORPORATE, legal_name=f"A Sitesi{letter}"
            )
            self.customer_b = Customer.objects.create(
                company=self.company, type=CustomerType.CORPORATE, legal_name=f"B Plaza{letter}"
            )
            building_a = Building.objects.create(
                company=self.company,
                customer=self.customer_a,
                name="A Blok",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
                neighborhood=moda,
            )
            building_b = Building.objects.create(
                company=self.company,
                customer=self.customer_b,
                name="B Blok",
                type=BuildingType.COMMERCIAL,
                address_note="x",
                neighborhood=levent,
            )
            self.a1 = make_elevator(self.company, building_a, f"A1{letter}")
            self.a2 = make_elevator(self.company, building_a, f"A2{letter}")
            self.b1 = make_elevator(self.company, building_b, f"B1{letter}")
            self.x1 = make_elevator(self.company, building_a, f"X1{letter}")

            self.tech_a.customer_assignments.create(company=self.company, customer=self.customer_a)
            for tech in (self.tech_b1, self.tech_b2):
                tech.customer_assignments.create(company=self.company, customer=self.customer_b)

            self.contract = Contract.objects.create(
                company=self.company,
                customer=self.customer_a,
                contract_number=f"C-1{letter}",
                status=ContractStatus.ACTIVE,
                scope=Scope.MAINTENANCE_ONLY,
                start_date=TODAY - timedelta(days=100),
                end_date=TODAY + timedelta(days=265),
                pricing_type=PricingType.FLAT,
                monthly_fee="1000.00",
                vat_rate="20.00",
            )
            self.lines = {
                elevator.pk: ContractElevator.objects.create(
                    company=self.company,
                    contract=self.contract,
                    elevator=elevator,
                    added_at=self.contract.start_date,
                )
                for elevator in (self.a1, self.a2, self.b1)
            }

    def row(self, elevator: Elevator, period: str = OCT) -> PlannedVisit:
        with company_context(self.company.id):
            return PlannedVisit.objects.select_related("visit").get(
                elevator=elevator, period=period
            )

    def rows(self) -> list[PlannedVisit]:
        with company_context(self.company.id):
            return list(PlannedVisit.objects.all())

    def set_next_date(self, elevator: Elevator, value: date | None) -> None:
        Elevator.unscoped.filter(pk=elevator.pk).update(next_maintenance_date=value)

    def drop_cover(self, elevator: Elevator) -> None:
        ContractElevator.unscoped.filter(pk=self.lines[elevator.pk].pk).update(removed_at=TODAY)


@pytest.fixture
def plan(db) -> Plan:
    return Plan()


def assigned_notifications(user: User) -> list[Notification]:
    return list(
        Notification.unscoped.filter(recipient=user, kind=NotificationKind.VISIT_ASSIGNED).order_by(
            "data__period", "created_at", "id"
        )
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class TestDueDate:
    @pytest.mark.parametrize(
        ("next_date", "due"),
        [
            # Never visited: the middle of the month.
            (None, date(2026, 10, 15)),
            # Inside the month: as it is.
            (date(2026, 10, 25), date(2026, 10, 25)),
            # Already overdue from last month: kept, not clamped to the 1st —
            # that the lift has been due since 28 September is the fact.
            (date(2026, 9, 28), date(2026, 9, 28)),
            # Past the month (a visit early in it moved the date): the last day.
            (date(2026, 11, 10), date(2026, 10, 31)),
        ],
    )
    def test_the_lifts_date_held_to_the_months_end(self, next_date, due):
        assert due_date_in(OCT, next_date) == due

    def test_february_ends_on_its_own_last_day(self):
        assert due_date_in("2027-02", date(2027, 3, 5)) == date(2027, 2, 28)


class TestGeneration:
    def test_this_month_and_next_for_covered_lifts_only(self, plan):
        result = plan_maintenance.call()

        assert result["created"] == 6
        rows = plan.rows()
        assert {(row.elevator_id, row.period) for row in rows} == {
            (elevator.pk, period)
            for elevator in (plan.a1, plan.a2, plan.b1)
            for period in (OCT, NOV)
        }
        assert {row.company_id for row in rows} == {plan.company.id}
        assert all(row.status == PlannedVisitStatus.PLANNED for row in rows)

    def test_due_dates(self, plan):
        plan.set_next_date(plan.a1, date(2026, 10, 25))
        plan.set_next_date(plan.a2, date(2026, 11, 10))
        plan_maintenance.call()

        assert plan.row(plan.a1, OCT).due_date == date(2026, 10, 25)
        # Due 25 October: November's row says so rather than "1 November".
        assert plan.row(plan.a1, NOV).due_date == date(2026, 10, 25)
        assert plan.row(plan.a2, OCT).due_date == date(2026, 10, 31)
        assert plan.row(plan.a2, NOV).due_date == date(2026, 11, 10)
        # Never visited.
        assert plan.row(plan.b1, OCT).due_date == date(2026, 10, 15)
        assert plan.row(plan.b1, NOV).due_date == date(2026, 11, 15)

    def test_the_only_technician_of_a_customer_is_the_default(self, plan):
        plan_maintenance.call()
        assert plan.row(plan.a1).technician_id == plan.tech_a.id
        assert plan.row(plan.a2, NOV).technician_id == plan.tech_a.id
        # Two technicians cover customer B: the office decides.
        assert plan.row(plan.b1).technician_id is None

    def test_an_inactive_technician_does_not_count(self, plan):
        plan.tech_b2.is_active = False
        plan.tech_b2.save(update_fields=["is_active"])
        plan_maintenance.call()
        assert plan.row(plan.b1).technician_id == plan.tech_b1.id

    def test_a_second_run_changes_nothing(self, plan):
        plan_maintenance.call()
        before = {(row.pk, row.updated_at) for row in plan.rows()}

        assert plan_maintenance.call() == {
            "created": 0,
            "updated": 0,
            "cancelled": 0,
            "notified": 0,
        }
        assert {(row.pk, row.updated_at) for row in plan.rows()} == before

    def test_a_lift_that_lost_cover_is_cancelled(self, plan):
        plan_maintenance.call()
        plan.drop_cover(plan.a2)

        result = plan_maintenance.call()
        assert result["cancelled"] == 2
        assert plan.row(plan.a2, OCT).status == PlannedVisitStatus.CANCELLED
        assert plan.row(plan.a2, NOV).status == PlannedVisitStatus.CANCELLED
        assert plan.row(plan.a1).status == PlannedVisitStatus.PLANNED

    def test_an_edited_row_is_never_cancelled_or_overwritten(self, plan):
        plan.set_next_date(plan.a1, date(2026, 10, 25))
        plan_maintenance.call()
        row = plan.row(plan.a1)
        PlannedVisit.unscoped.filter(pk=row.pk).update(manually_edited=True, technician=None)
        plan.set_next_date(plan.a1, date(2026, 10, 28))
        plan.drop_cover(plan.a1)

        plan_maintenance.call()
        row = plan.row(plan.a1)
        assert row.status == PlannedVisitStatus.PLANNED
        assert row.due_date == date(2026, 10, 25)
        assert row.technician_id is None

    def test_an_unedited_row_follows_the_lifts_date(self, plan):
        plan_maintenance.call()
        plan.set_next_date(plan.a1, date(2026, 10, 27))
        plan_maintenance.call()
        assert plan.row(plan.a1).due_date == date(2026, 10, 27)

    def test_a_lift_that_regains_cover_comes_back(self, plan):
        plan_maintenance.call()
        plan.drop_cover(plan.b1)
        plan_maintenance.call()
        ContractElevator.unscoped.filter(pk=plan.lines[plan.b1.pk].pk).update(removed_at=None)

        plan_maintenance.call()
        assert plan.row(plan.b1).status == PlannedVisitStatus.PLANNED

    def test_it_runs_across_firms(self, plan):
        other = Plan("B")
        plan_maintenance.call()
        assert len(plan.rows()) == 6
        assert {row.company_id for row in other.rows()} == {other.company.id}
        assert len(other.rows()) == 6

    def test_it_is_scheduled_at_one(self):
        (schedule,) = [s for s in scheduling.get_schedules() if s.name == "plan_maintenance"]
        assert schedule.at.hour == 1
        assert schedule.at.minute == 0


class TestGenerationNotifies:
    def test_one_per_technician_per_month(self, plan):
        plan_maintenance.call()

        sent = assigned_notifications(plan.tech_a)
        assert [(n.data, n.subject_type, n.subject_id) for n in sent] == [
            ({"period": OCT, "count": 2}, "plan", plan.tech_a.id),
            ({"period": NOV, "count": 2}, "plan", plan.tech_a.id),
        ]
        # Nothing was assigned to B's technicians.
        assert assigned_notifications(plan.tech_b1) == []

    def test_a_second_run_tells_nobody_again(self, plan):
        plan_maintenance.call()
        plan_maintenance.call()
        assert len(assigned_notifications(plan.tech_a)) == 2

    def test_it_is_emailed_in_turkish(self, plan, django_capture_on_commit_callbacks):
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            plan_maintenance.call()
        subjects = sorted(m.subject for m in django_mail.outbox if m.to == [plan.tech_a.email])
        assert subjects == ["2 bakım size atandı — Ekim 2026", "2 bakım size atandı — Kasım 2026"]


def test_tr_month():
    assert tr_month("2026-10") == "Ekim 2026"
    assert tr_month(date(2027, 2, 1)) == "Şubat 2027"
    assert tr_month("someday") == "someday"


# ---------------------------------------------------------------------------
# Closing a row from a visit
# ---------------------------------------------------------------------------


def record_visit(plan: Plan, elevator: Elevator, day: date, status: str = VisitStatus.COMPLETED):
    response = api_for(plan.owner).post(
        reverse("maintenance-visit-list"),
        {
            "elevator": str(elevator.id),
            "contract": str(plan.contract.id),
            "technician": str(plan.tech_a.id),
            "visit_date": day.isoformat(),
            "status": status,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 201, response.data
    return response.data["id"]


@pytest.fixture
def planned(plan) -> Plan:
    plan_maintenance.call()
    return plan


class TestClosing:
    def test_a_completed_visit_closes_the_row(self, planned):
        visit_id = record_visit(planned, planned.a1, date(2026, 10, 10))
        row = planned.row(planned.a1)
        assert row.status == PlannedVisitStatus.DONE
        assert str(row.visit_id) == visit_id
        # Next month's row is untouched by it.
        assert planned.row(planned.a1, NOV).status == PlannedVisitStatus.PLANNED

    @pytest.mark.parametrize("status", [VisitStatus.INCOMPLETE, VisitStatus.ACCESS_DENIED])
    def test_an_attempt_keeps_it_planned_and_is_linked(self, planned, status):
        visit_id = record_visit(planned, planned.a1, date(2026, 10, 10), status)
        row = planned.row(planned.a1)
        assert row.status == PlannedVisitStatus.PLANNED
        assert str(row.visit_id) == visit_id

    def test_a_later_attempt_does_not_undo_a_completion(self, planned):
        done = record_visit(planned, planned.a1, date(2026, 10, 5))
        record_visit(planned, planned.a1, date(2026, 10, 12), VisitStatus.ACCESS_DENIED)
        row = planned.row(planned.a1)
        assert row.status == PlannedVisitStatus.DONE
        assert str(row.visit_id) == done

    def test_deleting_the_visit_reopens_the_row(self, planned):
        visit_id = record_visit(planned, planned.a1, date(2026, 10, 10))
        response = api_for(planned.owner).delete(
            reverse("maintenance-visit-detail", args=[visit_id])
        )
        assert response.status_code == 204
        row = planned.row(planned.a1)
        assert row.status == PlannedVisitStatus.PLANNED
        assert row.visit_id is None

    def test_moving_the_date_to_another_month_moves_the_link(self, planned):
        with company_context(planned.company.id):
            september = PlannedVisit.objects.create(
                company=planned.company,
                elevator=planned.a1,
                period="2026-09",
                due_date=date(2026, 9, 15),
            )
        visit_id = record_visit(planned, planned.a1, date(2026, 10, 10))

        response = api_for(planned.owner).patch(
            reverse("maintenance-visit-detail", args=[visit_id]),
            {"visit_date": "2026-09-20"},
            format="json",
        )
        assert response.status_code == 200, response.data
        october = planned.row(planned.a1)
        assert october.status == PlannedVisitStatus.PLANNED
        assert october.visit_id is None
        september = planned.row(planned.a1, "2026-09")
        assert september.status == PlannedVisitStatus.DONE
        assert str(september.visit_id) == visit_id

    def test_a_visit_with_no_row_stands_on_its_own(self, plan):
        # No plan generated at all: an ad-hoc visit is recorded as before.
        record_visit(plan, plan.a1, date(2026, 10, 10))
        assert plan.rows() == []
        with company_context(plan.company.id):
            assert MaintenanceVisit.objects.count() == 1

    def test_a_cancelled_row_is_not_revived_by_a_visit(self, planned):
        row = planned.row(planned.a1)
        PlannedVisit.unscoped.filter(pk=row.pk).update(status=PlannedVisitStatus.CANCELLED)
        record_visit(planned, planned.a1, date(2026, 10, 10))
        assert planned.row(planned.a1).status == PlannedVisitStatus.CANCELLED


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def listed(client: APIClient, **params: str) -> list[dict[str, Any]]:
    response = client.get(reverse("planned-visit-list"), {"period": OCT, **params})
    assert response.status_code == 200, response.data
    return list(response.data["results"])


def names(rows: list[dict[str, Any]]) -> list[str]:
    return [row["elevator_name"] for row in rows]


class TestList:
    def test_the_period_is_required(self, planned):
        response = api_for(planned.owner).get(reverse("planned-visit-list"))
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "period", "code": "required"}]

    def test_route_order_and_the_names_the_screen_prints(self, planned):
        rows = listed(api_for(planned.owner))
        # Beşiktaş before Kadikoy, then building, then lift.
        assert names(rows) == ["B1", "A1", "A2"]
        first = rows[0]
        assert first["district_name"] == "Beşiktaş"
        assert first["neighborhood_name"] == "Levent"
        assert first["building_name"] == "B Blok"
        assert first["customer_name"] == "B Plaza"
        assert first["technician_name"] is None
        assert first["latest_visit_status"] is None
        # Never visited, so due on the 15th — five days ago.
        assert first["is_overdue"] is True

    def test_ordering_by_due_date(self, planned):
        planned.set_next_date(planned.a2, date(2026, 10, 3))
        plan_maintenance.call()
        rows = listed(api_for(planned.owner), ordering="due_date")
        assert names(rows)[0] == "A2"
        assert rows[0]["is_overdue"] is True

    def test_tried_not_done_is_visible(self, planned):
        record_visit(planned, planned.a1, date(2026, 10, 10), VisitStatus.ACCESS_DENIED)
        (row,) = [r for r in listed(api_for(planned.owner)) if r["elevator_name"] == "A1"]
        assert row["status"] == PlannedVisitStatus.PLANNED
        assert row["latest_visit_status"] == VisitStatus.ACCESS_DENIED

    def test_filters(self, planned):
        client = api_for(planned.owner)
        assert names(listed(client, technician=str(planned.tech_a.id))) == ["A1", "A2"]
        assert names(listed(client, unassigned="true")) == ["B1"]
        assert names(listed(client, district=str(planned.besiktas.id))) == ["B1"]
        assert names(listed(client, customer=str(planned.customer_a.id))) == ["A1", "A2"]
        assert names(listed(client, due_before="2026-10-15")) == ["B1", "A1", "A2"]
        assert names(listed(client, due_before="2026-10-14")) == []
        assert names(listed(client, status=PlannedVisitStatus.DONE)) == []
        assert len(listed(client)) == 3
        nov = client.get(reverse("planned-visit-list"), {"period": NOV}).data["results"]
        assert {row["period"] for row in nov} == {NOV}

    def test_a_malformed_technician_or_period_is_a_400(self, planned):
        client = api_for(planned.owner)
        bad_tech = client.get(reverse("planned-visit-list"), {"period": OCT, "technician": "x"})
        assert bad_tech.status_code == 400
        bad_period = client.get(reverse("planned-visit-list"), {"period": "2026-13"})
        assert bad_period.status_code == 400

    def test_technician_me(self, planned):
        assert names(listed(api_for(planned.tech_a), technician="me")) == ["A1", "A2"]
        assert listed(api_for(planned.owner), technician="me") == []

    def test_a_single_row_is_found_by_id_alone(self, planned):
        row = planned.row(planned.a1)
        response = api_for(planned.owner).get(reverse("planned-visit-detail", args=[row.pk]))
        assert response.status_code == 200
        assert response.data["elevator_name"] == "A1"


class TestTechnicianNarrowing:
    def test_own_rows_and_unassigned_rows_of_their_customers(self, planned):
        a2 = planned.row(planned.a2)
        PlannedVisit.unscoped.filter(pk=a2.pk).update(technician=None)
        a1 = planned.row(planned.a1)
        PlannedVisit.unscoped.filter(pk=a1.pk).update(technician=planned.tech_b1)

        # tech_a: A2 is unassigned and A's; A1 is now somebody else's; B is not theirs.
        assert names(listed(api_for(planned.tech_a))) == ["A2"]
        response = api_for(planned.tech_a).get(reverse("planned-visit-detail", args=[a1.pk]))
        assert response.status_code == 404
        # tech_b1 holds A1 but is not assigned to customer A: only B's open row.
        assert names(listed(api_for(planned.tech_b1))) == ["B1"]

    def test_a_technician_cannot_arrange_the_plan(self, planned):
        row = planned.row(planned.a1)
        client = api_for(planned.tech_a)
        patch = client.patch(
            reverse("planned-visit-detail", args=[row.pk]),
            {"planned_date": "2026-10-22"},
            format="json",
        )
        assert patch.status_code == 403
        bulk = client.post(
            reverse("planned-visit-bulk-assign"),
            {"ids": [str(row.pk)], "technician": str(planned.tech_a.id)},
            format="json",
        )
        assert bulk.status_code == 403

    def test_the_accountant_has_no_access(self, planned):
        response = api_for(planned.accountant).get(reverse("planned-visit-list"), {"period": OCT})
        assert response.status_code == 403
        assert response.data["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


class TestEditing:
    def test_operations_reassigns_and_the_row_is_marked_edited(self, planned):
        row = planned.row(planned.b1)
        response = api_for(planned.ops).patch(
            reverse("planned-visit-detail", args=[row.pk]),
            {"technician": str(planned.tech_b1.id), "planned_date": "2026-10-22"},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["technician"] == planned.tech_b1.id
        assert response.data["technician_name"] == "Banu X"
        assert response.data["planned_date"] == "2026-10-22"
        assert response.data["manually_edited"] is True
        assert [n.data for n in assigned_notifications(planned.tech_b1)] == [
            {"period": OCT, "count": 1}
        ]

    def test_a_date_only_edit_tells_nobody(self, planned):
        row = planned.row(planned.a1)
        before = len(assigned_notifications(planned.tech_a))
        api_for(planned.owner).patch(
            reverse("planned-visit-detail", args=[row.pk]),
            {"planned_date": "2026-10-22"},
            format="json",
        )
        assert planned.row(planned.a1).manually_edited is True
        assert len(assigned_notifications(planned.tech_a)) == before

    def test_a_user_of_another_firm_cannot_be_named(self, planned):
        other = Plan("B")
        row = planned.row(planned.a1)
        response = api_for(planned.owner).patch(
            reverse("planned-visit-detail", args=[row.pk]),
            {"technician": str(other.tech_a.id)},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "technician", "code": "does_not_exist"}
        ]
        assert planned.row(planned.a1).technician_id == planned.tech_a.id

    def test_an_unknown_field_is_refused(self, planned):
        row = planned.row(planned.a1)
        response = api_for(planned.owner).patch(
            reverse("planned-visit-detail", args=[row.pk]),
            {"status": PlannedVisitStatus.DONE},
            format="json",
        )
        assert response.status_code == 400
        assert planned.row(planned.a1).status == PlannedVisitStatus.PLANNED

    def test_there_is_no_create_or_delete(self, planned):
        row = planned.row(planned.a1)
        client = api_for(planned.owner)
        assert client.post(reverse("planned-visit-list"), {}, format="json").status_code == 405
        detail = reverse("planned-visit-detail", args=[row.pk])
        assert client.delete(detail).status_code == 405


class TestBulkAssign:
    def test_assigns_every_row_and_tells_the_technician_once(self, planned):
        rows = [planned.row(e, period) for e in (planned.a1, planned.b1) for period in (OCT, NOV)]
        response = api_for(planned.owner).post(
            reverse("planned-visit-bulk-assign"),
            {"ids": [str(row.pk) for row in rows], "technician": str(planned.tech_b2.id)},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data == {"count": 4}
        for row in [
            planned.row(e, period) for e in (planned.a1, planned.b1) for period in (OCT, NOV)
        ]:
            assert row.technician_id == planned.tech_b2.id
            assert row.manually_edited is True
        assert [n.data for n in assigned_notifications(planned.tech_b2)] == [
            {"period": OCT, "count": 2},
            {"period": NOV, "count": 2},
        ]

    def test_a_retry_tells_nobody_twice(self, planned):
        body = {"ids": [str(planned.row(planned.b1).pk)], "technician": str(planned.tech_b1.id)}
        client = api_for(planned.owner)
        client.post(reverse("planned-visit-bulk-assign"), body, format="json")
        again = client.post(reverse("planned-visit-bulk-assign"), body, format="json")
        assert again.status_code == 200
        assert len(assigned_notifications(planned.tech_b1)) == 1

    @pytest.mark.parametrize("intruder", ["foreign", "unknown"])
    def test_one_bad_id_refuses_the_lot(self, planned, intruder):
        if intruder == "foreign":
            other = Plan("B")
            plan_maintenance.call()
            bad = str(other.row(other.a1).pk)
        else:
            bad = str(uuid4())
        good = planned.row(planned.b1)
        response = api_for(planned.owner).post(
            reverse("planned-visit-bulk-assign"),
            {"ids": [str(good.pk), bad], "technician": str(planned.tech_b1.id)},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"][0]["field"].startswith("ids")
        assert planned.row(planned.b1).technician_id is None
        assert assigned_notifications(planned.tech_b1) == []

    def test_an_empty_list_is_refused(self, planned):
        response = api_for(planned.owner).post(
            reverse("planned-visit-bulk-assign"),
            {"ids": [], "technician": str(planned.tech_b1.id)},
            format="json",
        )
        assert response.status_code == 400


class TestSummary:
    def test_counts(self, planned):
        planned.set_next_date(planned.a2, date(2026, 10, 3))
        plan_maintenance.call()
        record_visit(planned, planned.a1, date(2026, 10, 10))

        response = api_for(planned.owner).get(reverse("planned-visit-summary"), {"period": OCT})
        assert response.status_code == 200, response.data
        assert response.data == {
            "period": OCT,
            "total": 3,
            "done": 1,
            "remaining": 2,
            "by_status": {"planned": 2, "done": 1, "cancelled": 0},
            "unassigned": 1,
            # B1 (due on the 15th) and A2 (due on the 3rd).
            "overdue": 2,
            "technicians": [
                {
                    "technician": str(planned.tech_a.id),
                    "technician_name": "Ali X",
                    "total": 2,
                    "done": 1,
                    "remaining": 1,
                    "overdue": 1,
                    "cancelled": 0,
                    "planned": 1,
                }
            ],
        }

    def test_the_period_is_required(self, planned):
        response = api_for(planned.owner).get(reverse("planned-visit-summary"))
        assert response.status_code == 400


class TestAcrossFirms:
    def test_another_firms_plan_is_invisible(self, planned):
        other = Plan("B")
        plan_maintenance.call()
        theirs = other.row(other.a1)

        client = api_for(planned.owner)
        assert names(listed(client)) == ["B1", "A1", "A2"]
        detail = client.get(reverse("planned-visit-detail", args=[theirs.pk]))
        assert detail.status_code == 404
        summary = client.get(reverse("planned-visit-summary"), {"period": OCT}).data
        assert summary["total"] == 3

    def test_period_of(self):
        assert period_of(date(2026, 1, 31)) == "2026-01"


class TestTheRowFollowsTheLift:
    """SL-162 (audit P1-01): a carried-over row keeps the lift's real due date,
    and moves when the lift's next date does."""

    def test_an_overdue_lift_keeps_its_real_due_date_and_says_how_late(self, plan):
        plan.set_next_date(plan.a1, date(2026, 9, 8))
        plan_maintenance.call()
        assert plan.row(plan.a1, OCT).due_date == date(2026, 9, 8)
        rows = listed(api_for(plan.owner))
        (a1,) = [row for row in rows if row["elevator_name"] == "A1"]
        assert a1["due_date"] == "2026-09-08"
        assert a1["is_overdue"] is True
        assert a1["days_overdue"] == 42
        (a2,) = [row for row in rows if row["elevator_name"] == "A2"]
        assert a2["days_overdue"] == 5  # never visited: due on the 15th

    def test_a_visit_in_another_month_moves_the_rows_of_this_one(self, plan):
        plan.set_next_date(plan.a1, date(2026, 9, 8))
        plan_maintenance.call()
        # A visit dated in September: it closes no October row, but the lift is
        # now next due on 30 October, and the October row has to say so.
        record_visit(plan, plan.a1, date(2026, 9, 30))
        october = plan.row(plan.a1, OCT)
        assert october.status == PlannedVisitStatus.PLANNED
        assert october.due_date == date(2026, 10, 30)
        assert plan.row(plan.a1, NOV).due_date == date(2026, 10, 30)

    def test_an_edited_row_keeps_its_date(self, plan):
        plan.set_next_date(plan.a1, date(2026, 9, 8))
        plan_maintenance.call()
        row = plan.row(plan.a1)
        PlannedVisit.unscoped.filter(pk=row.pk).update(manually_edited=True)
        record_visit(plan, plan.a1, date(2026, 9, 30))
        assert plan.row(plan.a1).due_date == date(2026, 9, 8)

    def test_the_daily_job_follows_the_lift_too(self, plan):
        plan_maintenance.call()
        Elevator.unscoped.filter(pk=plan.a2.pk).update(next_maintenance_date=date(2026, 10, 2))
        plan_maintenance.call()
        assert plan.row(plan.a2).due_date == date(2026, 10, 2)
