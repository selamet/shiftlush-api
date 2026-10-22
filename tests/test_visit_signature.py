"""The building's signature on a maintenance visit (spec 2026-10-22-visit-signature-design)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.urls import reverse
from django.utils import timezone

from apps.maintenance.models import MaintenanceVisit, VisitStatus
from tests import test_maintenance_visits
from tests.test_maintenance_visit_create import api_for, body, post

visit_fixture = test_maintenance_visits.visit_fixture

SIGNED = {
    "signature_status": "signed",
    "signer_name": "Ayşe Yılmaz",
    "signer_role": "manager",
    "signed_at": "2026-10-22T09:15:00+03:00",
}


def details(response: Any) -> list[dict[str, str]]:
    return list(response.data["error"]["details"])


def stored(response: Any) -> MaintenanceVisit:
    return MaintenanceVisit.unscoped.get(pk=response.data["id"])


class TestTheRecord:
    def test_a_completed_visit_needs_a_signature_status(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        payload = body(elevator, contract, owner)
        del payload["signature_status"]
        response = post(owner, payload)
        assert response.status_code == 400
        assert details(response) == [{"field": "signature_status", "code": "required"}]

    def test_an_incomplete_visit_may_leave_it_blank(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        payload = body(
            elevator, contract, owner, status=VisitStatus.INCOMPLETE, status_note="Elektrik yoktu"
        )
        del payload["signature_status"]
        response = post(owner, payload)
        assert response.status_code == 201, response.data
        assert stored(response).signature_status == ""

    def test_signed_needs_a_name_and_a_role(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        no_name = post(owner, body(elevator, contract, owner, signature_status="signed"), "k1")
        assert {"field": "signer_name", "code": "required"} in details(no_name)
        no_role = post(
            owner,
            body(elevator, contract, owner, signature_status="signed", signer_name="Ali"),
            "k2",
        )
        assert details(no_role) == [{"field": "signer_role", "code": "required"}]

    def test_a_reason_carries_no_name(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = post(
            owner,
            body(elevator, contract, owner, signature_status="not_present", signer_name="Ali"),
        )
        assert response.status_code == 400
        assert details(response) == [{"field": "signer_name", "code": "invalid"}]

    def test_signed_is_stored_with_its_time(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        response = post(owner, body(elevator, contract, owner, **SIGNED))
        assert response.status_code == 201, response.data
        visit = stored(response)
        assert (visit.signature_status, visit.signer_name, visit.signer_role) == (
            "signed",
            "Ayşe Yılmaz",
            "manager",
        )
        assert visit.signed_at == datetime.fromisoformat("2026-10-22T09:15:00+03:00")

    def test_a_future_signed_at_takes_the_server_time(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        ahead = (timezone.now() + timedelta(hours=1)).isoformat()
        response = post(owner, body(elevator, contract, owner, **{**SIGNED, "signed_at": ahead}))
        assert response.status_code == 201, response.data
        signed_at = stored(response).signed_at
        assert signed_at is not None
        assert abs((signed_at - timezone.now()).total_seconds()) < 30

    def test_a_missing_signed_at_takes_the_server_time(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        undated = {k: v for k, v in SIGNED.items() if k != "signed_at"}
        payload = body(elevator, contract, owner, **undated)
        response = post(owner, payload)
        assert response.status_code == 201, response.data
        signed_at = stored(response).signed_at
        assert signed_at is not None
        assert abs((signed_at - timezone.now()).total_seconds()) < 30


class TestEditsAfterSigning:
    def signed_visit(self, fixture: Any) -> MaintenanceVisit:
        _, owner, elevator, contract = fixture
        return stored(post(owner, body(elevator, contract, owner, **SIGNED)))

    def test_signature_fields_are_read_only_after_create(self, visit_fixture):
        owner = visit_fixture[1]
        visit = self.signed_visit(visit_fixture)
        response = api_for(owner).patch(
            reverse("maintenance-visit-detail", args=[visit.id]),
            {"signer_name": "X"},
            format="json",
        )
        assert response.status_code == 400
        assert details(response) == [{"field": "signer_name", "code": "read_only"}]
        visit = MaintenanceVisit.unscoped.get(pk=visit.pk)
        assert visit.signer_name == "Ayşe Yılmaz"

    def test_an_edit_after_signing_is_marked(self, visit_fixture):
        owner = visit_fixture[1]
        visit = self.signed_visit(visit_fixture)
        url = reverse("maintenance-visit-detail", args=[visit.id])
        assert api_for(owner).patch(url, {"work_notes": "fixed"}, format="json").status_code == 200
        visit = MaintenanceVisit.unscoped.get(pk=visit.pk)
        assert visit.amended_after_signature is True
        api_for(owner).patch(url, {"work_notes": "again"}, format="json")
        visit = MaintenanceVisit.unscoped.get(pk=visit.pk)
        assert visit.amended_after_signature is True

    def test_an_edit_before_any_signature_is_not_marked(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        visit = stored(
            post(
                owner,
                body(elevator, contract, owner, status=VisitStatus.INCOMPLETE, status_note="x"),
            )
        )
        api_for(owner).patch(
            reverse("maintenance-visit-detail", args=[visit.id]), {"work_notes": "y"}, format="json"
        )
        visit = MaintenanceVisit.unscoped.get(pk=visit.pk)
        assert visit.amended_after_signature is False
