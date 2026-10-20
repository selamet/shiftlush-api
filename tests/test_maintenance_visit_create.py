"""Recording a visit in one request that can be retried safely.

M1 (docs/superpowers/specs/2026-10-20-m1-maintenance-visit-mobile-design.md):
the phone sends a visit from an outbox, possibly several times, on a connection
that drops. So the create needs an idempotency key, carries its checklist with
it, and either records everything or nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)
from apps.users.models import User
from apps.users.services import issue_tokens
from core.error_codes import ErrorCode
from tests import test_maintenance_visits

visit_fixture = test_maintenance_visits.visit_fixture


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


def body(elevator: Any, contract: Any, owner: User, **extra: Any) -> dict[str, Any]:
    return {
        "elevator": str(elevator.id),
        "contract": str(contract.id),
        "technician": str(owner.id),
        "visit_date": str(date.today()),
        "status": VisitStatus.COMPLETED,
        **extra,
    }


def post(user: User, payload: dict[str, Any], key: str | None = "visit-key-1") -> Any:
    client = api_for(user)
    if key:
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}", HTTP_IDEMPOTENCY_KEY=key
        )
    return client.post(reverse("maintenance-visit-list"), payload, format="json")


FULL_CHECKLIST = [{"item": item, "result": ChecklistResult.OK} for item in ChecklistItem.values]


class TestTheKeyIsRequired:
    def test_a_create_without_one_is_refused_and_records_nothing(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = post(owner, body(elevator, contract, owner), key=None)

        assert response.status_code == 400, response.data
        assert response.data["error"]["code"] == ErrorCode.IDEMPOTENCY_KEY_REQUIRED.value
        assert not MaintenanceVisit.unscoped.exists()

    def test_a_retry_with_the_same_key_returns_the_same_visit(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        payload = body(elevator, contract, owner, items=FULL_CHECKLIST)

        first = post(owner, payload)
        second = post(owner, payload)

        assert first.status_code == second.status_code == 201
        assert first.data["id"] == second.data["id"]
        assert MaintenanceVisit.unscoped.count() == 1
        assert MaintenanceVisitItem.unscoped.filter(is_deleted=False).count() == 16

    def test_other_creates_still_accept_a_request_without_one(self, visit_fixture):
        # Only the visit is retried from an outbox. Making every create demand
        # a key would break every client for no reason.
        _, owner, *_ = visit_fixture
        response = api_for(owner).post(
            reverse("customer-list"),
            {"type": "corporate", "legal_name": "No Key Ltd"},
            format="json",
        )
        assert response.status_code != 400 or (
            response.data["error"]["code"] != ErrorCode.IDEMPOTENCY_KEY_REQUIRED.value
        )


class TestTheChecklistTravelsWithTheVisit:
    def test_items_sent_on_create_are_recorded_and_returned(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        items = [
            {"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK},
            {
                "item": ChecklistItem.CAR_LIGHTING,
                "result": ChecklistResult.ATTENTION_NEEDED,
                "note": "two bulbs out",
            },
        ]

        response = post(owner, body(elevator, contract, owner, items=items))

        assert response.status_code == 201, response.data
        returned = {row["item"]: row for row in response.data["items"]}
        assert set(returned) == {ChecklistItem.BRAKE_SYSTEM, ChecklistItem.CAR_LIGHTING}
        assert returned[ChecklistItem.CAR_LIGHTING]["note"] == "two bulbs out"

    def test_a_visit_without_items_is_still_accepted(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = post(owner, body(elevator, contract, owner))
        assert response.status_code == 201, response.data
        assert response.data["items"] == []

    def test_the_same_item_twice_refuses_the_whole_visit(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        twice = [
            {"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.OK},
            {"item": ChecklistItem.BRAKE_SYSTEM, "result": ChecklistResult.ATTENTION_NEEDED},
        ]

        response = post(owner, body(elevator, contract, owner, items=twice))

        assert response.status_code == 400, response.data
        assert response.data["error"]["details"][0]["field"] == "items"
        assert not MaintenanceVisit.unscoped.exists()

    def test_an_unknown_item_refuses_the_whole_visit(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = post(
            owner,
            body(elevator, contract, owner, items=[{"item": "flux_capacitor", "result": "ok"}]),
        )
        assert response.status_code == 400, response.data
        assert not MaintenanceVisit.unscoped.exists()


@pytest.mark.django_db(transaction=True)
class TestAllOrNothing:
    def test_a_failure_after_the_visit_is_written_leaves_nothing_behind(self, visit_fixture):
        # The visit row, its checklist and the elevator's dates are three
        # writes. If the last one fails, the first two must not survive it.
        _, owner, elevator, contract = visit_fixture
        with mock.patch(
            "apps.maintenance.api.v1.views.services.recalculate_next_maintenance",
            side_effect=RuntimeError("disk full"),
        ):
            client = api_for(owner)
            client.raise_request_exception = False
            client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {issue_tokens(owner).access}",
                HTTP_IDEMPOTENCY_KEY="atomic-1",
            )
            response = client.post(
                reverse("maintenance-visit-list"),
                body(elevator, contract, owner, items=FULL_CHECKLIST),
                format="json",
            )

        assert response.status_code == 500
        assert not MaintenanceVisit.unscoped.exists()
        assert not MaintenanceVisitItem.unscoped.exists()


class TestARetryDaysLaterIsStillTheSameVisit:
    """#99: a phone can sit offline for days with a visit in its outbox.

    If the first send reached the server and only the response was lost, the
    retry arrives whenever the phone next has signal. With the default 24-hour
    key, a retry on day three found no key and recorded the visit again.
    """

    def test_the_key_outlives_a_long_offline_stretch(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        payload = body(elevator, contract, owner, items=FULL_CHECKLIST)
        first = post(owner, payload, key="offline-for-days")

        later = timezone.now() + timedelta(days=21)
        with mock.patch("core.idempotency.timezone.now", return_value=later):
            retry = post(owner, payload, key="offline-for-days")

        assert retry.status_code == 201
        assert retry.data["id"] == first.data["id"]
        assert MaintenanceVisit.unscoped.count() == 1

    def test_the_visit_key_is_kept_for_thirty_days(self, visit_fixture):
        from core.models import IdempotencyKey

        _, owner, elevator, contract = visit_fixture
        post(owner, body(elevator, contract, owner), key="kept")

        stored = IdempotencyKey.objects.get(key="kept")
        assert stored.expires_at - stored.created_at > timedelta(days=30) - timedelta(minutes=1)

    def test_other_creates_keep_the_default_window(self):
        # Only the endpoint a device outbox retries needs the long window; a
        # month of stored responses for every create would be a table nobody
        # needed.
        from apps.customers.api.v1.views import CustomerViewSet
        from core.idempotency import RETENTION, retention_for

        assert retention_for(CustomerViewSet()) == RETENTION
