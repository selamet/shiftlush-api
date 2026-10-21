"""Periodic inspections — M5, slice 1.

docs/superpowers/specs/2026-10-20-m5-periodic-inspection-design.md. The rules
come first and are read from apps/inspections/rules.py, the same table the
code uses: a rule change is one edit there, and these follow it. Then what an
inspection does to the lift, the non-conformity → fault action, the API, the
report PDF, and the two daily jobs. "Today" is pinned to 2026-10-20.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core import mail as django_mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.contracts.models import ContractElevator
from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults.models import Fault, FaultPriority
from apps.inspections import rules
from apps.inspections.models import (
    Inspection,
    InspectionKind,
    Label,
    NonConformity,
    NonConformityStatus,
)
from apps.inspections.tasks import correction_deadline_notice, inspection_due_notice
from apps.notifications.models import Notification, NotificationKind, NotificationPreference
from core import scheduling
from core.context import company_context
from core.error_codes import ErrorCode
from tests.test_faults import Shop, api_for

TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def today() -> Iterator[date]:
    with (
        mock.patch("apps.inspections.tasks.local_today", return_value=TODAY),
        mock.patch("apps.inspections.services.local_today", return_value=TODAY),
        mock.patch("apps.inspections.api.v1.serializers.local_today", return_value=TODAY),
        mock.patch("apps.inspections.api.v1.views.local_today", return_value=TODAY),
    ):
        yield TODAY


@pytest.fixture
def shop(db) -> Shop:
    return Shop()


def record(shop: Shop, by: Any = None, **body: Any) -> Any:
    payload = {
        "elevator": str(shop.elevator.id),
        "inspected_on": TODAY.isoformat(),
        "inspection_body": "Ornek Muayene A.S.",
        "report_number": "R-1",
        "label": Label.GREEN,
        **body,
    }
    return api_for(by or shop.owner).post(reverse("inspection-list"), payload, format="json")


def recorded(shop: Shop, **body: Any) -> dict[str, Any]:
    response = record(shop, **body)
    assert response.status_code == 201, response.data
    return dict(response.data)


def lift(shop: Shop) -> Elevator:
    return Elevator.unscoped.get(pk=shop.elevator.pk)


def told(kind: str) -> set[str]:
    return {
        n.recipient.email
        for n in Notification.unscoped.filter(kind=kind).select_related("recipient")
    }


def nc(label: str = Label.YELLOW, text: str = "Kapi kilidi arizali") -> dict[str, str]:
    return {"description": text, "severity": label}


# ---------------------------------------------------------------------------
# The rules, as data
# ---------------------------------------------------------------------------


class TestTheRuleTable:
    def test_every_label_has_a_rule_with_a_citation(self):
        assert set(rules.RULES) == set(Label)
        for rule in rules.RULES.values():
            assert "RG 30411" in rule.citation
            assert "31952" in rule.citation

    def test_only_red_forbids_use(self):
        assert {label for label, rule in rules.RULES.items() if not rule.usable} == {Label.RED}

    @pytest.mark.parametrize("label", list(Label))
    def test_the_deadline_is_what_the_table_says(self, shop, label):
        body = recorded(shop, label=label, inspected_on="2026-09-01")
        rule = rules.RULES[label]
        inspected = date(2026, 9, 1)
        if rule.deadline == rules.Deadline.DAYS:
            expected = (inspected + timedelta(days=rule.days or 0)).isoformat()
        elif rule.deadline == rules.Deadline.NEXT_INSPECTION:
            expected = body["next_inspection_due"]
        else:
            expected = None
        assert body["correction_deadline"] == expected
        assert body["lift_usable"] is rule.usable
        assert body["sealed_if_missed"] is rule.sealed_if_missed

    def test_the_numbers_from_the_regulation(self):
        # Restated once, so that an edit to the table is a visible decision.
        assert rules.RULES[Label.YELLOW].days == 120
        assert rules.RULES[Label.RED].days == 60
        assert rules.RULES[Label.BLUE].deadline == rules.Deadline.NEXT_INSPECTION
        assert rules.RULES[Label.GREEN].deadline == rules.Deadline.NONE

    def test_the_next_inspection_is_a_year_on(self, shop):
        body = recorded(shop, inspected_on="2026-09-01")
        assert body["next_inspection_due"] == "2027-09-01"
        assert rules.default_next_inspection(date(2024, 2, 29)) == date(2025, 2, 28)

    def test_days_left(self, shop):
        body = recorded(shop, label=Label.RED, inspected_on="2026-10-10")
        assert body["correction_deadline"] == "2026-12-09"
        assert body["days_left"] == 50


class TestFollowUps:
    def test_red_turned_yellow_counts_120_days_from_the_original(self, shop):
        original = recorded(shop, label=Label.RED, inspected_on="2026-08-01")
        follow_up = recorded(
            shop,
            kind=InspectionKind.FOLLOW_UP,
            follows=original["id"],
            label=Label.YELLOW,
            inspected_on="2026-09-25",
        )
        assert follow_up["correction_deadline"] == "2026-11-29"  # 1 Aug + 120 days
        # And the periodic cycle is the original's, not a year from the follow-up.
        assert follow_up["next_inspection_due"] == original["next_inspection_due"]

    def test_a_follow_up_needs_the_inspection_it_follows(self, shop):
        response = record(shop, kind=InspectionKind.FOLLOW_UP)
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "follows", "code": "required"}]

    def test_it_must_follow_the_same_lift(self, shop):
        other = Shop("B")
        with company_context(shop.company.id):
            second = Elevator.objects.create(
                company=shop.company, building=shop.building, qr_token="z" * 24, name="Sag"
            )
        original = recorded(shop, label=Label.RED)
        response = record(
            shop,
            elevator=str(second.id),
            kind=InspectionKind.FOLLOW_UP,
            follows=original["id"],
        )
        assert response.status_code == 400
        assert other.company.id != shop.company.id

    def test_a_periodic_inspection_follows_nothing(self, shop):
        original = recorded(shop, label=Label.RED)
        assert record(shop, follows=original["id"]).status_code == 400


# ---------------------------------------------------------------------------
# What an inspection does to the lift
# ---------------------------------------------------------------------------


class TestTheLatestInspectionDrivesTheElevator:
    def test_the_five_fields_are_copied(self, shop):
        recorded(
            shop,
            label=Label.BLUE,
            inspected_on="2026-10-01",
            inspection_body="TSE",
            report_number="2026/77",
            next_inspection_due="2027-09-15",
        )
        elevator = lift(shop)
        assert elevator.last_inspection_date == date(2026, 10, 1)
        assert elevator.inspection_label == Label.BLUE
        assert elevator.next_inspection_date == date(2027, 9, 15)
        assert elevator.inspection_body == "TSE"
        assert elevator.inspection_report_number == "2026/77"

    def test_an_older_inspection_recorded_later_does_not_win(self, shop):
        recorded(shop, label=Label.GREEN, inspected_on="2026-10-01")
        recorded(shop, label=Label.YELLOW, inspected_on="2025-10-01")
        assert lift(shop).inspection_label == Label.GREEN

    def test_a_correction_moves_the_elevator_too(self, shop):
        body = recorded(shop, label=Label.GREEN)
        api_for(shop.owner).patch(
            reverse("inspection-detail", args=[body["id"]]),
            {"label": Label.BLUE},
            format="json",
        )
        assert lift(shop).inspection_label == Label.BLUE

    def test_the_elevator_form_no_longer_writes_them(self, shop):
        response = api_for(shop.owner).patch(
            reverse("elevator-detail", args=[shop.elevator.id]),
            {"inspection_label": Label.GREEN, "next_inspection_date": "2027-01-01"},
            format="json",
        )
        assert response.status_code == 400
        assert lift(shop).inspection_label != Label.GREEN

    def test_the_elevator_still_reads_them(self, shop):
        recorded(shop, label=Label.BLUE)
        body = api_for(shop.owner).get(reverse("elevator-detail", args=[shop.elevator.id])).data
        assert body["inspection_label"] == Label.BLUE


class TestARedLabel:
    def test_takes_an_active_lift_out_of_service_and_says_so(self, shop):
        recorded(shop, label=Label.RED, nonconformities=[nc(Label.RED)])
        assert lift(shop).status == ElevatorStatus.OUT_OF_SERVICE
        assert told(NotificationKind.INSPECTION_RED_LABEL) == {
            shop.owner.email,
            shop.admin.email,
            shop.ops.email,
            shop.tech.email,
            shop.tech2.email,
        }

    def test_is_emailed_even_to_somebody_who_turned_it_off(
        self, shop, django_capture_on_commit_callbacks
    ):
        with company_context(shop.company.id):
            NotificationPreference.objects.create(
                company=shop.company,
                user=shop.owner,
                kind=NotificationKind.INSPECTION_RED_LABEL,
                email=False,
            )
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            recorded(shop, label=Label.RED)
        assert shop.owner.email in {m.to[0] for m in django_mail.outbox}

    def test_a_later_good_result_puts_it_back(self, shop):
        recorded(shop, label=Label.RED, inspected_on="2026-09-01")
        recorded(shop, label=Label.YELLOW, inspected_on="2026-10-01")
        assert lift(shop).status == ElevatorStatus.ACTIVE

    def test_a_sealed_lift_stays_sealed(self, shop):
        recorded(shop, label=Label.RED, inspected_on="2026-09-01")
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(status=ElevatorStatus.SEALED)
        recorded(shop, label=Label.GREEN, inspected_on="2026-10-01")
        assert lift(shop).status == ElevatorStatus.SEALED

    def test_a_correction_to_red_tells_people_once(self, shop):
        body = recorded(shop, label=Label.YELLOW)
        url = reverse("inspection-detail", args=[body["id"]])
        api_for(shop.owner).patch(url, {"label": Label.RED}, format="json")
        api_for(shop.owner).patch(url, {"report_number": "R-2"}, format="json")
        assert (
            Notification.unscoped.filter(
                kind=NotificationKind.INSPECTION_RED_LABEL, recipient=shop.owner
            ).count()
            == 1
        )
        assert lift(shop).status == ElevatorStatus.OUT_OF_SERVICE


# ---------------------------------------------------------------------------
# Non-conformities, and the fault that fixes one
# ---------------------------------------------------------------------------


def first_nc(body: dict[str, Any]) -> str:
    return str(body["nonconformities"][0]["id"])


class TestNonConformities:
    def test_recorded_with_the_inspection(self, shop):
        body = recorded(shop, label=Label.YELLOW, nonconformities=[nc(), nc(text="Pit kirli")])
        assert [row["description"] for row in body["nonconformities"]] == [
            "Kapi kilidi arizali",
            "Pit kirli",
        ]
        assert {row["status"] for row in body["nonconformities"]} == {NonConformityStatus.OPEN}

    def test_added_afterwards(self, shop):
        body = recorded(shop, label=Label.YELLOW)
        response = api_for(shop.ops).post(
            reverse("inspection-nonconformities", args=[body["id"]]), nc(), format="json"
        )
        assert response.status_code == 201, response.data
        assert str(response.data["inspection"]) == body["id"]

    def test_fixed_means_fixed_today_unless_dated(self, shop):
        nc_id = first_nc(recorded(shop, label=Label.YELLOW, nonconformities=[nc()]))
        url = reverse("nonconformity-detail", args=[nc_id])
        client = api_for(shop.ops)
        fixed = client.patch(url, {"status": NonConformityStatus.FIXED}, format="json")
        assert fixed.status_code == 200, fixed.data
        assert fixed.data["fixed_on"] == TODAY.isoformat()
        reopened = client.patch(url, {"status": NonConformityStatus.OPEN}, format="json")
        assert reopened.data["fixed_on"] is None
        dated = client.patch(
            url, {"status": NonConformityStatus.FIXED, "fixed_on": "2026-10-05"}, format="json"
        )
        assert dated.data["fixed_on"] == "2026-10-05"

    def test_a_technician_reads_but_does_not_edit(self, shop):
        nc_id = first_nc(recorded(shop, label=Label.YELLOW, nonconformities=[nc()]))
        url = reverse("nonconformity-detail", args=[nc_id])
        assert api_for(shop.tech).get(url).status_code == 200
        response = api_for(shop.tech).patch(url, {"status": "fixed"}, format="json")
        assert response.status_code == 403
        assert api_for(shop.outsider).get(url).status_code == 404


class TestOpeningAFault:
    @pytest.mark.parametrize(
        ("severity", "priority"),
        [(Label.RED, FaultPriority.HIGH), (Label.YELLOW, FaultPriority.NORMAL)],
    )
    def test_opens_a_linked_fault(self, shop, severity, priority):
        nc_id = first_nc(recorded(shop, label=severity, nonconformities=[nc(severity)]))
        response = api_for(shop.ops).post(reverse("nonconformity-open-fault", args=[nc_id]))
        assert response.status_code == 201, response.data
        fault = Fault.unscoped.get(pk=response.data["fault"])
        assert fault.priority == priority
        assert fault.elevator_id == shop.elevator.id
        assert fault.description == "Kapi kilidi arizali"
        assert fault.reported_by_id == shop.ops.id
        # The fault is an ordinary M4 fault: the office hears about it.
        assert shop.owner.email in told(NotificationKind.FAULT_REPORTED)

    def test_a_second_tap_returns_the_same_fault(self, shop):
        nc_id = first_nc(recorded(shop, label=Label.YELLOW, nonconformities=[nc()]))
        url = reverse("nonconformity-open-fault", args=[nc_id])
        first = api_for(shop.ops).post(url)
        second = api_for(shop.ops).post(url)
        assert second.status_code == 200
        assert second.data["fault"] == first.data["fault"]
        assert Fault.unscoped.count() == 1

    def test_the_office_only(self, shop):
        nc_id = first_nc(recorded(shop, label=Label.YELLOW, nonconformities=[nc()]))
        url = reverse("nonconformity-open-fault", args=[nc_id])
        assert api_for(shop.tech).post(url).status_code == 403
        assert api_for(shop.accountant).post(url).status_code == 403
        assert not Fault.unscoped.exists()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestTheApi:
    def test_who_may_do_what(self, shop):
        assert record(shop, by=shop.tech).status_code == 403
        assert record(shop, by=shop.accountant).status_code == 403
        assert api_for(shop.accountant).get(reverse("inspection-list")).status_code == 403
        body = recorded(shop, by=shop.ops)
        rows = api_for(shop.tech).get(reverse("inspection-list")).data["results"]
        assert [row["id"] for row in rows] == [body["id"]]
        assert api_for(shop.outsider).get(reverse("inspection-list")).data["results"] == []

    def test_there_is_no_delete(self, shop):
        body = recorded(shop)
        response = api_for(shop.owner).delete(reverse("inspection-detail", args=[body["id"]]))
        assert response.status_code == 405

    def test_the_deadline_is_not_the_clients(self, shop):
        response = record(shop, correction_deadline="2030-01-01")
        assert response.status_code == 400

    def test_filters(self, shop):
        green = recorded(shop, label=Label.GREEN, inspected_on="2025-10-01")
        yellow = recorded(
            shop, label=Label.YELLOW, inspected_on="2026-10-01", nonconformities=[nc()]
        )
        client = api_for(shop.owner)
        url = reverse("inspection-list")

        def ids(**params: str) -> list[str]:
            return [row["id"] for row in client.get(url, params).data["results"]]

        assert ids(label=Label.YELLOW) == [yellow["id"]]
        assert ids(has_open_nonconformities="true") == [yellow["id"]]
        assert ids(has_open_nonconformities="false") == [green["id"]]
        assert ids(due_before="2026-12-31") == [green["id"]]
        assert ids(elevator=str(shop.elevator.id)) == [yellow["id"], green["id"]]

    def test_the_list_does_not_query_per_row(self, shop):
        client = api_for(shop.owner)
        recorded(shop, label=Label.YELLOW, nonconformities=[nc()])
        with CaptureQueriesContext(connection) as one:
            client.get(reverse("inspection-list"))
        for year in range(2021, 2025):
            recorded(
                shop,
                label=Label.BLUE,
                inspected_on=f"{year}-05-05",
                nonconformities=[nc(Label.BLUE), nc(Label.BLUE)],
            )
        with CaptureQueriesContext(connection) as five:
            response = client.get(reverse("inspection-list"))
        assert len(response.data["results"]) == 5
        assert len(five) == len(one)

    def test_another_firms_inspection_is_out_of_reach(self, shop):
        other = Shop("B")
        theirs = recorded(other, label=Label.YELLOW, nonconformities=[nc()])
        client = api_for(shop.owner)
        assert client.get(reverse("inspection-list")).data["results"] == []
        response = client.post(reverse("nonconformity-open-fault", args=[first_nc(theirs)]))
        assert response.status_code == 404
        assert not Fault.unscoped.exists()


class TestWhatIsDue:
    def test_inspections_and_corrections_by_date(self, shop):
        recorded(
            shop,
            label=Label.YELLOW,
            inspected_on="2026-07-01",
            next_inspection_due="2026-11-30",
            nonconformities=[nc()],
        )
        rows = api_for(shop.owner).get(reverse("inspection-due"), {"days": "60"}).data
        assert [(row["type"], row["date"]) for row in rows] == [
            ("correction", "2026-10-29"),  # 1 Jul + 120 days
            ("inspection", "2026-11-30"),
        ]
        correction = rows[0]
        assert correction["days_left"] == 9
        assert correction["open_nonconformities"] == 1
        assert correction["sealed_if_missed"] is True
        assert correction["elevator_name"] == "Sol"

    def test_a_window_too_short_leaves_them_out(self, shop):
        recorded(shop, label=Label.GREEN, next_inspection_due="2026-12-30")
        assert api_for(shop.owner).get(reverse("inspection-due"), {"days": "30"}).data == []

    def test_overdue_stays_on_the_list(self, shop):
        recorded(shop, label=Label.RED, inspected_on="2026-07-01", nonconformities=[nc()])
        rows = api_for(shop.owner).get(reverse("inspection-due"), {"days": "0"}).data
        assert [(row["type"], row["days_left"]) for row in rows] == [("correction", -51)]

    def test_a_correction_with_nothing_open_is_not_due(self, shop):
        body = recorded(shop, label=Label.RED, inspected_on="2026-09-01", nonconformities=[nc()])
        api_for(shop.ops).patch(
            reverse("nonconformity-detail", args=[first_nc(body)]),
            {"status": NonConformityStatus.FIXED},
            format="json",
        )
        rows = api_for(shop.owner).get(reverse("inspection-due")).data
        assert [row["type"] for row in rows] == []

    def test_narrowed_for_technicians(self, shop):
        recorded(shop, label=Label.RED, inspected_on="2026-09-01", nonconformities=[nc()])
        assert len(api_for(shop.tech).get(reverse("inspection-due")).data) == 1
        assert api_for(shop.outsider).get(reverse("inspection-due")).data == []


# ---------------------------------------------------------------------------
# The report PDF
# ---------------------------------------------------------------------------


def attachment_for(shop: Shop, object_id: Any, category: str, **extra: Any) -> Attachment:
    with company_context(shop.company.id):
        return Attachment.objects.create(
            company=shop.company,
            object_type=extra.pop("object_type", ObjectType.INSPECTION),
            object_id=object_id,
            category=category,
            original_filename="rapor.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            storage_key=f"{shop.company.id}/inspection/{object_id}/{category}/x.pdf",
        )


class TestTheReport:
    def test_linked_by_its_own_inspection_report(self, shop):
        body = recorded(shop)
        pdf = attachment_for(shop, body["id"], AttachmentCategory.INSPECTION_REPORT)
        response = api_for(shop.owner).patch(
            reverse("inspection-detail", args=[body["id"]]), {"report": str(pdf.id)}, format="json"
        )
        assert response.status_code == 200, response.data
        assert response.data["report"] == pdf.id

    @pytest.mark.parametrize("wrong", ["another_inspection", "category", "object_type"])
    def test_anything_else_is_refused(self, shop, wrong):
        body = recorded(shop)
        other = recorded(shop, inspected_on="2025-10-20")
        if wrong == "another_inspection":
            pdf = attachment_for(shop, other["id"], AttachmentCategory.INSPECTION_REPORT)
        elif wrong == "category":
            pdf = attachment_for(shop, body["id"], AttachmentCategory.PHOTO)
        else:
            pdf = attachment_for(
                shop,
                body["id"],
                AttachmentCategory.INSPECTION_REPORT,
                object_type=ObjectType.ELEVATOR,
            )
        response = api_for(shop.owner).patch(
            reverse("inspection-detail", args=[body["id"]]), {"report": str(pdf.id)}, format="json"
        )
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.ATTACHMENT_TARGET_MISMATCH.value
        assert Inspection.unscoped.get(pk=body["id"]).report_id is None

    def test_another_firms_file_does_not_exist(self, shop):
        other = Shop("B")
        theirs = attachment_for(
            other, uuid_of(recorded(other)), AttachmentCategory.INSPECTION_REPORT
        )
        body = recorded(shop)
        response = api_for(shop.owner).patch(
            reverse("inspection-detail", args=[body["id"]]),
            {"report": str(theirs.id)},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "report", "code": "does_not_exist"}]


def uuid_of(body: dict[str, Any]) -> Any:
    return body["id"]


# ---------------------------------------------------------------------------
# The daily jobs
# ---------------------------------------------------------------------------


class TestInspectionDueNotice:
    def test_thirty_days_ahead_to_the_office(self, shop):
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(
            next_inspection_date=TODAY + timedelta(days=30)
        )
        assert inspection_due_notice.call() == 3
        assert told(NotificationKind.INSPECTION_DUE) == {
            shop.owner.email,
            shop.admin.email,
            shop.ops.email,
        }
        assert inspection_due_notice.call() == 0

    def test_not_yet(self, shop):
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(
            next_inspection_date=TODAY + timedelta(days=31)
        )
        assert inspection_due_notice.call() == 0

    def test_not_for_a_lift_the_firm_does_not_maintain(self, shop):
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(
            next_inspection_date=TODAY + timedelta(days=10)
        )
        ContractElevator.unscoped.filter(pk=shop.line.pk).update(removed_at=TODAY)
        assert inspection_due_notice.call() == 0


def yellow_due_in(shop: Shop, days: int, *, open_ncs: int = 1) -> dict[str, Any]:
    inspected = TODAY + timedelta(days=days) - timedelta(days=120)
    return recorded(
        shop,
        label=Label.YELLOW,
        inspected_on=inspected.isoformat(),
        nonconformities=[nc() for _ in range(open_ncs)],
    )


def near(shop: Shop) -> list[dict[str, Any]]:
    return [
        n.data
        for n in Notification.unscoped.filter(
            kind=NotificationKind.CORRECTION_DEADLINE_NEAR, recipient=shop.owner
        ).order_by("created_at", "id")
    ]


class TestCorrectionDeadlineNotice:
    @pytest.mark.parametrize(("days", "warned"), [(31, False), (30, True), (20, True), (3, True)])
    def test_warns_from_thirty_days(self, shop, days, warned):
        yellow_due_in(shop, days)
        correction_deadline_notice.call()
        assert bool(near(shop)) is warned

    def test_once_per_threshold(self, shop):
        body = yellow_due_in(shop, 20)
        correction_deadline_notice.call()
        correction_deadline_notice.call()
        assert [n["days_left"] for n in near(shop)] == [20]
        # Thirteen days later, the 14-day threshold is crossed: a new warning.
        Inspection.unscoped.filter(pk=body["id"]).update(
            correction_deadline=TODAY + timedelta(days=7)
        )
        correction_deadline_notice.call()
        assert [n["days_left"] for n in near(shop)] == [20, 7]

    def test_reaches_the_customers_technicians(self, shop):
        yellow_due_in(shop, 3)
        correction_deadline_notice.call()
        recipients = told(NotificationKind.CORRECTION_DEADLINE_NEAR)
        assert {shop.tech.email, shop.tech2.email, shop.ops.email} <= recipients
        assert shop.outsider.email not in recipients

    def test_a_passed_deadline_is_a_sealing_risk_emailed_regardless(
        self, shop, django_capture_on_commit_callbacks
    ):
        yellow_due_in(shop, -1)
        with company_context(shop.company.id):
            NotificationPreference.objects.create(
                company=shop.company,
                user=shop.owner,
                kind=NotificationKind.CORRECTION_DEADLINE_PASSED,
                email=False,
            )
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            correction_deadline_notice.call()
        assert shop.owner.email in told(NotificationKind.CORRECTION_DEADLINE_PASSED)
        assert shop.owner.email in {m.to[0] for m in django_mail.outbox}
        assert correction_deadline_notice.call() == 0

    def test_nothing_while_nothing_is_open(self, shop):
        yellow_due_in(shop, 3, open_ncs=0)
        assert correction_deadline_notice.call() == 0

    def test_a_later_inspection_replaces_the_deadline(self, shop):
        yellow_due_in(shop, 3)
        recorded(shop, label=Label.GREEN, inspected_on=TODAY.isoformat())
        assert correction_deadline_notice.call() == 0

    def test_a_follow_up_still_counts_the_originals_open_defects(self, shop):
        original = recorded(
            shop, label=Label.RED, inspected_on="2026-07-01", nonconformities=[nc(Label.RED)]
        )
        # Turned yellow by a follow-up that lists no defects of its own: the
        # original's are still open, and the deadline is 1 Jul + 120 = 29 Oct.
        recorded(
            shop,
            kind=InspectionKind.FOLLOW_UP,
            follows=original["id"],
            label=Label.YELLOW,
            inspected_on="2026-09-01",
        )
        correction_deadline_notice.call()
        assert [(n["days_left"], n["open_nonconformities"]) for n in near(shop)] == [(9, 1)]

    def test_both_jobs_are_scheduled(self):
        names = {s.name for s in scheduling.get_schedules()}
        assert {"inspection_due_notice", "correction_deadline_notice"} <= names


def test_nonconformity_model_keeps_its_fault(shop):
    nc_id = first_nc(recorded(shop, label=Label.RED, nonconformities=[nc(Label.RED)]))
    api_for(shop.ops).post(reverse("nonconformity-open-fault", args=[nc_id]))
    with company_context(shop.company.id):
        row = NonConformity.objects.select_related("fault").get(pk=nc_id)
    assert row.fault is not None
    assert row.fault.priority == FaultPriority.HIGH


class TestTheOpenCorrectionOnTheElevator:
    """SL-162 (audit P1-06): the legal deadline, not only the next inspection."""

    def test_list_detail_and_portal_carry_it(self, shop):
        body = recorded(
            shop, label=Label.RED, inspected_on="2026-10-10", nonconformities=[nc(Label.RED)]
        )
        detail = api_for(shop.owner).get(reverse("elevator-detail", args=[shop.elevator.id])).data
        assert detail["correction_deadline"] == "2026-12-09"
        assert detail["correction_days_left"] == 50
        assert detail["open_nonconformities"] == 1
        rows = api_for(shop.owner).get(reverse("elevator-list")).data["results"]
        (row,) = [r for r in rows if r["id"] == str(shop.elevator.id)]
        assert (row["correction_deadline"], row["open_nonconformities"]) == ("2026-12-09", 1)

        from apps.customers.models import CustomerContact
        from apps.portal import services as portal_services

        with company_context(shop.company.id):
            contact = CustomerContact.objects.create(
                company=shop.company, customer=shop.customer, full_name="Y", email="y@example.com"
            )
            _, token = portal_services.invite(customer=shop.customer, contact=contact)
        account = portal_services.accept(token=token, password="correct-horse-battery")
        from tests.test_portal import portal_for

        portal = portal_for(account).get(reverse("portal-elevator-detail", args=[shop.elevator.id]))
        assert portal.data["correction_deadline"] == "2026-12-09"
        assert portal.data["open_nonconformities"] == 1
        assert body["id"]

    def test_nothing_once_every_defect_is_fixed(self, shop):
        body = recorded(shop, label=Label.YELLOW, nonconformities=[nc()])
        api_for(shop.ops).patch(
            reverse("nonconformity-detail", args=[first_nc(body)]),
            {"status": NonConformityStatus.FIXED},
            format="json",
        )
        detail = api_for(shop.owner).get(reverse("elevator-detail", args=[shop.elevator.id])).data
        assert detail["correction_deadline"] is None
        assert detail["correction_days_left"] is None
        assert detail["open_nonconformities"] == 0

    def test_a_follow_up_counts_the_originals_defects(self, shop):
        original = recorded(
            shop, label=Label.RED, inspected_on="2026-07-01", nonconformities=[nc(Label.RED)]
        )
        recorded(
            shop,
            kind=InspectionKind.FOLLOW_UP,
            follows=original["id"],
            label=Label.YELLOW,
            inspected_on="2026-09-01",
        )
        detail = api_for(shop.owner).get(reverse("elevator-detail", args=[shop.elevator.id])).data
        assert detail["correction_deadline"] == "2026-10-29"
        assert detail["open_nonconformities"] == 1

    def test_the_list_does_not_query_per_lift(self, shop):
        client = api_for(shop.owner)
        recorded(shop, label=Label.RED, nonconformities=[nc(Label.RED)])
        with CaptureQueriesContext(connection) as one:
            client.get(reverse("elevator-list"))
        with company_context(shop.company.id):
            for n in range(4):
                lift = Elevator.objects.create(
                    company=shop.company, building=shop.building, qr_token=f"{n}" * 24, name=f"L{n}"
                )
                from apps.inspections import services as inspection_services

                inspection_services.record(
                    elevator=lift,
                    inspected_on=TODAY,
                    label=Label.YELLOW,
                    by=shop.owner,
                    nonconformities=[{"description": "x", "severity": Label.YELLOW}],
                )
        with CaptureQueriesContext(connection) as five:
            client.get(reverse("elevator-list"))
        assert len(five) == len(one)
