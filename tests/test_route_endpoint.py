"""A technician's day, in the order that drives least (calendar spec, part B)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.urls import reverse

from apps.companies.models import Company
from apps.maintenance.models import PlannedVisit, PlannedVisitStatus
from apps.maintenance.tasks import plan_maintenance
from apps.properties.models import Building, Complex
from core.context import company_context
from tests.test_planned_visits import TODAY, Plan, api_for, today  # noqa: F401 - autouse fixture


@pytest.fixture
def day(db) -> Plan:
    """A1, A2 (A Blok) and B1 (B Blok) all given to tech_a for today."""
    plan = Plan()
    plan_maintenance.call()
    for elevator in (plan.a1, plan.a2, plan.b1):
        PlannedVisit.unscoped.filter(elevator=elevator, period="2026-10").update(
            technician=plan.tech_a, planned_date=TODAY
        )
    return plan


def place(elevator: Any, latitude: str | None, longitude: str | None) -> None:
    Building.unscoped.filter(pk=elevator.building_id).update(latitude=latitude, longitude=longitude)


def route(client: Any, **params: str) -> Any:
    return client.get(reverse("planned-visit-route"), params)


def names(response: Any) -> list[str]:
    return [stop["elevator_name"] for stop in response.data["stops"]]


class TestRoute:
    def test_the_day_starts_at_the_firm_and_drives_least(self, day):
        place(day.a1, "39.900000", "41.200000")
        place(day.b1, "39.900000", "41.260000")
        Company.objects.filter(pk=day.company.pk).update(
            latitude="39.900000", longitude="41.270000"
        )
        response = route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-20")
        assert response.status_code == 200, response.data
        assert names(response) == ["B1", "A1", "A2"]
        assert response.data["stops"][0]["leg_km"] == pytest.approx(0.85, abs=0.05)
        assert response.data["stops"][2]["leg_km"] == 0
        assert response.data["total_km"] == pytest.approx(6.0, abs=0.1)
        assert response.data["unplaced"] == []
        assert response.data["stops"][0]["latitude"] == "39.900000"

    def test_a_stop_without_a_position_comes_last(self, day):
        place(day.a1, "39.900000", "41.200000")
        response = route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-20")
        assert names(response)[-1] == "B1"
        b1 = response.data["stops"][-1]
        assert response.data["unplaced"] == [b1["id"]]
        assert b1["latitude"] is None and b1["leg_km"] is None

    def test_today_includes_what_is_due_with_no_day(self, day):
        PlannedVisit.unscoped.filter(elevator=day.b1, period="2026-10").update(
            planned_date=None, due_date=TODAY - timedelta(days=3)
        )
        assert "B1" in names(
            route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-20")
        )
        tomorrow = route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-21")
        assert "B1" not in names(tomorrow)

    def test_cancelled_rows_are_not_on_the_road(self, day):
        PlannedVisit.unscoped.filter(elevator=day.a2, period="2026-10").update(
            status=PlannedVisitStatus.CANCELLED
        )
        response = route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-20")
        assert "A2" not in names(response)

    def test_a_technician_reads_their_own_day(self, day):
        response = route(api_for(day.tech_a), technician="me", date="2026-10-20")
        assert response.status_code == 200
        # B1 is customer B's, which tech_a is not assigned to: the usual narrowing.
        assert names(response) == ["A1", "A2"]

    def test_another_technicians_day_is_empty_to_a_technician(self, day):
        response = route(api_for(day.tech_b1), technician=str(day.tech_a.id), date="2026-10-20")
        assert response.status_code == 200
        assert response.data["stops"] == []

    @pytest.mark.parametrize("missing", ["technician", "date"])
    def test_both_parameters_are_required(self, day, missing):
        params = {"technician": "me", "date": "2026-10-20"}
        del params[missing]
        response = route(api_for(day.ops), **params)
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": missing, "code": "required"}]

    def test_a_bad_date_is_refused(self, day):
        response = route(api_for(day.ops), technician="me", date="20.10.2026")
        assert response.status_code == 400

    def test_a_technician_that_is_not_an_id_is_refused(self, day):
        response = route(api_for(day.ops), technician="ahmet", date="2026-10-20")
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "technician", "code": "invalid"}]

    def test_an_accountant_has_no_route(self, day):
        from apps.users.models import Role
        from tests.test_planned_visits import make_user

        accountant = make_user(day.company, Role.ACCOUNTANT, "route-accountant@example.com")
        assert route(api_for(accountant), technician="me", date="2026-10-20").status_code == 403


class TestEstateFallback:
    def give_b_blok_an_estate(self, day: Plan, building_latitude: str | None) -> None:
        with company_context(day.company.id):
            estate = Complex.objects.create(
                company=day.company,
                customer=day.customer_b,
                name="B Sitesi",
                latitude="39.900000",
                longitude="41.260000",
            )
        Building.unscoped.filter(pk=day.b1.building_id).update(
            complex=estate, latitude=building_latitude, longitude=None
        )

    def test_a_half_set_building_falls_back_to_its_estate(self, day):
        self.give_b_blok_an_estate(day, building_latitude="39.950000")
        response = route(api_for(day.ops), technician=str(day.tech_a.id), date="2026-10-20")
        b1 = next(stop for stop in response.data["stops"] if stop["elevator_name"] == "B1")
        assert (b1["latitude"], b1["longitude"]) == ("39.900000", "41.260000")

    def test_has_location_counts_the_estates_position(self, day):
        self.give_b_blok_an_estate(day, building_latitude=None)
        client = api_for(day.owner)
        missing = client.get(reverse("building-list"), {"has_location": "false"})
        located = client.get(reverse("building-list"), {"has_location": "true"})
        assert [b["name"] for b in missing.data["results"]] == ["A Blok"]
        assert [b["name"] for b in located.data["results"]] == ["B Blok"]


class TestHasLocation:
    def test_buildings_filter_by_whether_they_have_a_position(self, day):
        place(day.a1, "39.900000", "41.200000")
        client = api_for(day.owner)
        located = client.get(reverse("building-list"), {"has_location": "true"})
        missing = client.get(reverse("building-list"), {"has_location": "false"})
        assert [b["name"] for b in located.data["results"]] == ["A Blok"]
        assert [b["name"] for b in missing.data["results"]] == ["B Blok"]


def test_today_is_pinned():
    assert TODAY == date(2026, 10, 20)
