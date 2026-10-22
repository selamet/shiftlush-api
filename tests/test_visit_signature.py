"""The building's signature on a maintenance visit (spec 2026-10-22-visit-signature-design)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.maintenance.models import MaintenanceVisit, VisitStatus
from apps.users.models import User
from core.context import company_context
from tests import test_maintenance_attachments, test_maintenance_visits, test_portal
from tests.test_attachments import uploaded
from tests.test_maintenance_visit_create import api_for, body, post

visit_fixture = test_maintenance_visits.visit_fixture
a_visit = test_maintenance_attachments.a_visit
bucket = test_maintenance_attachments.bucket
world = test_portal.world

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


# The image -------------------------------------------------------------------


PNG = "image/png"


def signature_ticket(client: Any, visit: MaintenanceVisit, **overrides: Any) -> Any:
    from apps.attachments.models import AttachmentCategory, ObjectType

    payload = {
        "object_type": ObjectType.MAINTENANCE_VISIT,
        "object_id": str(visit.id),
        "category": AttachmentCategory.SIGNATURE,
        "mime_type": PNG,
        "size_bytes": 20_000,
        **overrides,
    }
    return client.post(reverse("attachment-upload-url"), payload)


def confirm(client: Any, key: str) -> Any:
    return client.post(
        reverse("attachment-list"), {"storage_key": key, "original_filename": "imza.png"}
    )


def mark(visit: MaintenanceVisit, status: str) -> None:
    MaintenanceVisit.unscoped.filter(pk=visit.pk).update(signature_status=status)


class TestSignatureImage:
    @pytest.fixture
    def signed(self, a_visit: Any) -> tuple[Any, MaintenanceVisit, Any]:
        company, visit = a_visit
        mark(visit, "signed")
        return company, visit, api_for(visit.technician)

    def test_the_technician_uploads_the_signature_of_their_signed_visit(self, signed, bucket):
        from apps.users.models import Role

        company, visit, _ = signed
        with company_context(company.id):
            technician = User.objects.create_user(
                company=company,
                email="sig-tech@example.com",
                password="correct-horse-battery",
                first_name="T",
                last_name="Tech",
                role=Role.TECHNICIAN,
            )
            technician.customer_assignments.create(
                company=company, customer=visit.elevator.building.customer
            )
        client = api_for(technician)
        ticket = signature_ticket(client, visit)
        assert ticket.status_code == 200, ticket.data
        uploaded(bucket, ticket.data["storage_key"], size=20_000, content_type=PNG)
        assert confirm(client, ticket.data["storage_key"]).status_code == 201

    def test_only_png_and_small(self, signed):
        _, visit, client = signed
        jpeg = signature_ticket(client, visit, mime_type="image/jpeg")
        assert (jpeg.status_code, jpeg.data["error"]["code"]) == (422, "UNSUPPORTED_MIME_TYPE")
        big = signature_ticket(client, visit, size_bytes=300_000)
        assert (big.status_code, big.data["error"]["code"]) == (422, "FILE_TOO_LARGE")

    def test_upload_url_refuses_a_signature_for_an_unsigned_visit(self, signed):
        _, visit, client = signed
        mark(visit, "not_present")
        response = signature_ticket(client, visit)
        assert (response.status_code, response.data["error"]["code"]) == (
            409,
            "SIGNATURE_NOT_EXPECTED",
        )

    def test_confirm_refuses_a_signature_for_an_unsigned_visit(self, signed, bucket):
        _, visit, client = signed
        key = signature_ticket(client, visit).data["storage_key"]
        uploaded(bucket, key, size=20_000, content_type=PNG)
        mark(visit, "declined")
        response = confirm(client, key)
        assert (response.status_code, response.data["error"]["code"]) == (
            409,
            "SIGNATURE_NOT_EXPECTED",
        )

    def test_a_second_signature_is_refused(self, signed, bucket):
        _, visit, client = signed
        for index in (1, 2):
            key = signature_ticket(client, visit).data["storage_key"]
            uploaded(bucket, key, size=20_000, content_type=PNG)
            response = confirm(client, key)
            if index == 1:
                assert response.status_code == 201, response.data
        assert (response.status_code, response.data["error"]["code"]) == (409, "SIGNATURE_EXISTS")

    def test_a_retried_confirm_is_the_same_signature(self, signed, bucket):
        from apps.attachments.models import Attachment

        _, visit, client = signed
        key = signature_ticket(client, visit).data["storage_key"]
        uploaded(bucket, key, size=20_000, content_type=PNG)
        first, second = confirm(client, key), confirm(client, key)
        assert (first.status_code, second.status_code) == (201, 201)
        assert first.data["id"] == second.data["id"]
        assert Attachment.unscoped.filter(object_id=visit.id).count() == 1

    def test_nobody_deletes_a_signature(self, signed, bucket):
        _, visit, client = signed
        key = signature_ticket(client, visit).data["storage_key"]
        uploaded(bucket, key, size=20_000, content_type=PNG)
        signature = confirm(client, key).data["id"]
        response = client.delete(reverse("attachment-detail", args=[signature]))
        assert (response.status_code, response.data["error"]["code"]) == (
            409,
            "SIGNATURE_CANNOT_BE_DELETED",
        )
        photo_ticket = signature_ticket(client, visit, category="photo", mime_type="image/jpeg")
        uploaded(bucket, photo_ticket.data["storage_key"])
        photo = confirm(client, photo_ticket.data["storage_key"]).data["id"]
        assert client.delete(reverse("attachment-detail", args=[photo])).status_code == 204


# Reading -----------------------------------------------------------------------


class TestReading:
    @pytest.fixture
    def five(self, world: Any) -> dict[str, Any]:
        """The portal world's own visit, signed with its image, and four more."""
        from apps.attachments.models import ObjectType

        site = world["ours"]
        shop = world["shop"]
        MaintenanceVisit.unscoped.filter(pk=site.visit.pk).update(
            signature_status="signed",
            signer_name="Ayşe Yılmaz",
            signer_role="manager",
            signed_at=timezone.now(),
        )
        with company_context(shop.company.id):
            site.signature = site._file(ObjectType.MAINTENANCE_VISIT, site.visit.pk, "signature")
            others = {}
            for state, status in (
                ("pending", "signed"),
                ("not_present", "not_present"),
                ("declined", "declined"),
                ("none", ""),
            ):
                visit = MaintenanceVisit.objects.create(
                    company=shop.company,
                    elevator=site.visit.elevator,
                    contract=site.visit.contract,
                    technician=site.visit.technician,
                    visit_date=site.visit.visit_date,
                    status=VisitStatus.COMPLETED,
                    signature_status=status,
                    signer_name="Ali" if status == "signed" else "",
                    signer_role="caretaker" if status == "signed" else "",
                )
                others[state] = visit
        return {"world": world, "signed": site.visit, **others}

    def office(self, five: dict[str, Any]) -> Any:
        return api_for(five["world"]["shop"].owner)

    def test_the_states(self, five):
        rows = self.office(five).get(reverse("maintenance-visit-list"), {"page_size": 100}).data
        by_id = {row["id"]: row["signature_state"] for row in rows["results"]}
        for state in ("signed", "pending", "not_present", "declined", "none"):
            assert by_id[str(five[state].pk)] == state, state

    def test_the_list_filters_by_state(self, five):
        client = self.office(five)
        states = ("signed", "pending", "not_present", "declined", "none")
        ours = {str(five[state].pk): state for state in states}
        for state in states:
            rows = client.get(
                reverse("maintenance-visit-list"), {"signature": state, "page_size": 100}
            ).data
            # The portal world has other visits of the firm; only ours are known.
            found = {row["id"] for row in rows["results"]} & ours.keys()
            assert found == {str(five[state].pk)}, state

    def test_the_list_has_no_n_plus_one(self, five, django_assert_max_num_queries):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self.office(five)
        with CaptureQueriesContext(connection) as one:
            client.get(reverse("maintenance-visit-list"), {"page_size": 1})
        with CaptureQueriesContext(connection) as five_rows:
            client.get(reverse("maintenance-visit-list"), {"page_size": 5})
        assert len(five_rows) == len(one)

    def test_portal_and_office_read_the_same_state(self, five):
        from tests.test_portal import portal_for

        site = five["world"]["ours"]
        office = (
            self.office(five).get(reverse("maintenance-visit-detail", args=[site.visit.pk])).data
        )
        portal = (
            portal_for(site.account).get(reverse("portal-visit-detail", args=[site.visit.pk])).data
        )
        assert office["signature_state"] == portal["signature_state"] == "signed"
        assert office["signer_name"] == portal["signer_name"] == "Ayşe Yılmaz"
        assert str(portal["signature"]["id"]) == str(site.signature.pk)
        assert [str(p["id"]) for p in portal["photos"]] == [str(site.photo.pk)]
