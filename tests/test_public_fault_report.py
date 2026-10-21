"""`POST /public/elevators/{qr_token}/faults/` — the only write without a token.

M4 spec, "Public report from the QR sticker". Every promise of its threat model
is a test here: what the answer does not say, the honeypot, the two throttles,
the 30-minute merge, the priority a resident can and cannot set, and that a
sticker reaches exactly one firm.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection, connections
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults.api.v1.public_views import PublicFaultReportView
from apps.faults.models import Fault, FaultChannel, FaultPriority, FaultStatus
from apps.notifications.models import Notification, NotificationKind
from tests.test_faults import Shop

ADDRESS = "203.0.113.7"


@pytest.fixture
def shop(db) -> Shop:
    return Shop()


def url(token: str) -> str:
    return reverse("public-fault-report", args=[token])


def send(token: str, address: str = ADDRESS, client: APIClient | None = None, **body: Any) -> Any:
    payload = {"description": "Asansör katlar arasında durdu", **body}
    return (client or APIClient()).post(url(token), payload, format="json", REMOTE_ADDR=address)


def faults() -> list[Fault]:
    return list(Fault.unscoped.order_by("reported_at", "id"))


class TestTheAnswer:
    def test_202_with_a_reference_and_nothing_else(self, shop):
        response = send(shop.elevator.qr_token, reporter_name="Ayşe", reporter_phone="0532")

        assert response.status_code == 202
        assert set(response.data) == {"reference"}
        (fault,) = faults()
        assert response.data["reference"] == fault.reference
        # Nothing about the firm, the building or the lift, anywhere in it.
        raw = response.content.decode() + json.dumps(dict(response.headers))
        for secret in (
            shop.company.display_name,
            shop.company.legal_name,
            str(shop.company.id),
            shop.building.name,
            shop.elevator.name,
            str(shop.elevator.id),
            shop.customer.legal_name,
        ):
            assert secret not in raw

    def test_the_fault_lands_in_the_stickers_firm(self, shop):
        send(shop.elevator.qr_token, reporter_name="Ayşe", reporter_phone="05321234567")
        (fault,) = faults()
        assert fault.company_id == shop.company.id
        assert fault.elevator_id == shop.elevator.id
        assert fault.channel == FaultChannel.PUBLIC_QR
        assert fault.status == FaultStatus.OPEN
        assert fault.reported_by_id is None
        assert (fault.reporter_name, fault.reporter_phone) == ("Ayşe", "05321234567")

    def test_name_and_phone_are_optional(self, shop):
        assert send(shop.elevator.qr_token).status_code == 202
        (fault,) = faults()
        assert (fault.reporter_name, fault.reporter_phone) == ("", "")

    def test_a_stale_token_in_the_browser_does_not_get_in_the_way(self, shop):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        assert send(shop.elevator.qr_token, client=client).status_code == 202

    def test_an_unknown_sticker_is_a_404_and_stores_nothing(self, shop):
        response = send("x" * 24)
        assert response.status_code == 404
        assert faults() == []

    def test_a_deleted_lift_is_unknown(self, shop):
        Elevator.unscoped.filter(pk=shop.elevator.pk).update(is_deleted=True)
        assert send(shop.elevator.qr_token).status_code == 404

    def test_a_description_is_required(self, shop):
        response = APIClient().post(url(shop.elevator.qr_token), {}, format="json")
        assert response.status_code == 400
        assert faults() == []

    def test_the_office_is_told(self, shop):
        send(shop.elevator.qr_token)
        recipients = {
            n.recipient_id
            for n in Notification.unscoped.filter(kind=NotificationKind.FAULT_REPORTED)
        }
        assert recipients == {
            shop.owner.id,
            shop.admin.id,
            shop.ops.id,
            shop.tech.id,
            shop.tech2.id,
        }
        (notification,) = Notification.unscoped.filter(recipient=shop.owner)
        assert notification.data["channel"] == FaultChannel.PUBLIC_QR

    def test_replay_protection_is_exempt_and_says_so(self):
        # Idempotency-Key is keyed by user and firm; this caller has neither.
        assert getattr(PublicFaultReportView.post, "replay_exempt", False) is True


class TestTheHoneypot:
    def test_a_bot_gets_the_same_answer_and_nothing_is_stored(self, shop):
        response = send(shop.elevator.qr_token, website="https://spam.example")
        assert response.status_code == 202
        assert set(response.data) == {"reference"}
        assert len(response.data["reference"]) == 6
        assert faults() == []
        assert not Notification.unscoped.exists()


class TestPriority:
    @pytest.mark.parametrize(
        ("body", "priority"),
        [
            ({}, FaultPriority.NORMAL),
            ({"elevator_stopped": True}, FaultPriority.HIGH),
            ({"someone_trapped": True}, FaultPriority.EMERGENCY),
            ({"someone_trapped": True, "elevator_stopped": True}, FaultPriority.EMERGENCY),
        ],
    )
    def test_from_what_the_resident_says(self, shop, body, priority):
        send(shop.elevator.qr_token, **body)
        (fault,) = faults()
        assert fault.priority == priority

    def test_priority_is_not_a_field_the_public_sets(self, shop):
        send(shop.elevator.qr_token, priority=FaultPriority.EMERGENCY)
        (fault,) = faults()
        assert fault.priority == FaultPriority.NORMAL

    def test_a_stopped_lift_goes_out_of_service(self, shop):
        send(shop.elevator.qr_token, elevator_stopped=True)
        assert Elevator.unscoped.get(pk=shop.elevator.pk).status == ElevatorStatus.OUT_OF_SERVICE

    def test_a_trapped_passenger_is_an_emergency_notice(self, shop):
        send(shop.elevator.qr_token, someone_trapped=True)
        assert Notification.unscoped.filter(kind=NotificationKind.FAULT_EMERGENCY).exists()
        assert not Notification.unscoped.filter(kind=NotificationKind.FAULT_REPORTED).exists()


class TestTheMerge:
    def test_ten_residents_make_one_fault(self, shop):
        first = send(shop.elevator.qr_token, address="198.51.100.1", reporter_name="Ayşe")
        second = send(shop.elevator.qr_token, address="198.51.100.2", reporter_name="Mehmet")

        assert second.data["reference"] == first.data["reference"]
        (fault,) = faults()
        assert len(fault.additional_reports) == 1
        note = fault.additional_reports[0]
        assert note["description"] == "Asansör katlar arasında durdu"
        # The first reporter's contact stays; the second's is not copied anywhere.
        assert fault.reporter_name == "Ayşe"
        assert "Mehmet" not in json.dumps(fault.additional_reports)
        # Told once, not twice.
        assert Notification.unscoped.filter(recipient=shop.owner).count() == 1

    def test_a_merged_report_fills_in_a_missing_contact(self, shop):
        send(shop.elevator.qr_token)
        send(shop.elevator.qr_token, reporter_name="Mehmet", reporter_phone="0555")
        (fault,) = faults()
        assert (fault.reporter_name, fault.reporter_phone) == ("Mehmet", "0555")

    def test_a_merged_report_can_raise_the_priority_and_says_so(self, shop):
        send(shop.elevator.qr_token)
        send(shop.elevator.qr_token, someone_trapped=True)
        (fault,) = faults()
        assert fault.priority == FaultPriority.EMERGENCY
        assert Notification.unscoped.filter(kind=NotificationKind.FAULT_EMERGENCY).exists()

    def test_it_never_lowers_it(self, shop):
        send(shop.elevator.qr_token, someone_trapped=True)
        send(shop.elevator.qr_token)
        (fault,) = faults()
        assert fault.priority == FaultPriority.EMERGENCY

    def test_after_30_minutes_it_is_a_new_fault(self, shop):
        send(shop.elevator.qr_token)
        Fault.unscoped.update(reported_at=timezone.now() - timedelta(minutes=31))
        send(shop.elevator.qr_token)
        assert len(faults()) == 2

    def test_only_into_an_open_or_assigned_report(self, shop):
        send(shop.elevator.qr_token)
        Fault.unscoped.update(status=FaultStatus.ON_SITE)
        send(shop.elevator.qr_token)
        assert len(faults()) == 2

    def test_not_into_a_staff_report(self, shop):
        shop.report()
        send(shop.elevator.qr_token)
        assert {f.channel for f in faults()} == {FaultChannel.STAFF, FaultChannel.PUBLIC_QR}


class TestTheThrottles:
    def test_five_an_hour_per_sticker(self, shop):
        token = shop.elevator.qr_token
        for n in range(5):
            assert send(token, address=f"198.51.100.{n}").status_code == 202
        # A sixth, from yet another address: the sticker has had its share.
        refused = send(token, address="198.51.100.99")
        assert refused.status_code == 429
        assert "Retry-After" in refused.headers

    def test_twenty_an_hour_per_address(self, shop):
        # Unknown stickers count too: walking a list of tokens is the abuse.
        for n in range(20):
            assert send(f"unknown-token-{n:02d}").status_code == 404
        assert send(shop.elevator.qr_token).status_code == 429
        assert faults() == []

    def test_the_quota_is_reported(self, shop):
        response = send(shop.elevator.qr_token)
        assert response.headers["RateLimit-Limit"] == "5"
        assert response.headers["RateLimit-Remaining"] == "4"


class TestOneStickerOneFirm:
    def test_another_firms_sticker_reaches_only_that_firm(self, shop):
        other = Shop("B")
        send(other.elevator.qr_token)
        (fault,) = faults()
        assert fault.company_id == other.company.id
        assert not Notification.unscoped.filter(recipient__company=shop.company).exists()
        assert Notification.unscoped.filter(recipient=other.owner).exists()

    def test_a_report_never_merges_across_firms(self, shop):
        other = Shop("B")
        send(shop.elevator.qr_token)
        send(other.elevator.qr_token)
        assert len(faults()) == 2
        assert {f.company_id for f in faults()} == {shop.company.id, other.company.id}


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="SQLite serialises every write; the race only exists on PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
class TestTwoFirstReportsAtOnce:
    """Two residents tap the sticker in the same instant (#M4 known gap).

    Both used to find no open fault and open one each. The lift's row is now
    locked first, so the second waits for the first and merges into it.
    """

    def test_they_make_one_fault(self):
        shop = Shop()
        start = threading.Barrier(2)
        answers: list[int] = []

        def resident(n: int) -> None:
            try:
                start.wait()
                answers.append(send(shop.elevator.qr_token, address=f"198.51.100.{n}").status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=resident, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert answers == [202, 202]
        assert Fault.unscoped.count() == 1


class TestATrappedPassengerNeedsNoWords:
    """SL-162 (audit P2-20): the tick is the whole report."""

    def test_trapped_without_a_description_is_accepted(self, shop):
        response = APIClient().post(
            url(shop.elevator.qr_token), {"someone_trapped": True}, format="json"
        )
        assert response.status_code == 202, response.data
        (fault,) = faults()
        assert fault.priority == FaultPriority.EMERGENCY
        assert fault.description == ""

    @pytest.mark.parametrize("body", [{}, {"description": "  "}, {"elevator_stopped": True}])
    def test_otherwise_a_description_is_required(self, shop, body):
        response = APIClient().post(url(shop.elevator.qr_token), body, format="json")
        assert response.status_code == 400
        assert response.data["error"]["details"] == [{"field": "description", "code": "required"}]
        assert faults() == []
