"""The customer portal — M6, slice 1.

docs/superpowers/specs/2026-10-21-m6-customer-portal-design.md. What matters
most is tested first and hardest:

- **tokens never cross:** a staff token opens no portal endpoint and a portal
  token opens no staff endpoint, swept over every routed view;
- **customer scope:** a contact sees their own customer and nothing of another
  customer of the same firm, or of another firm;
- **field exposure:** the portal's answers never carry the office's notes, the
  billable flag, money, technical data or staff names beyond a first name.

Then the account's life (invitation, accept, login, refresh, logout, reset,
lockout, deactivation), fault reporting, downloads and the office endpoints.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core import mail as django_mail
from django.urls import URLResolver, get_resolver, reverse
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient

from apps.attachments.models import Attachment, ObjectType
from apps.contracts.models import Contract
from apps.customers.models import Customer, CustomerContact, CustomerType
from apps.elevators.models import Elevator, ElevatorStatus
from apps.elevators.services import assign_qr_token
from apps.faults.models import Fault, FaultChannel, FaultPriority
from apps.inspections.models import Inspection, Label, NonConformity
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    VisitStatus,
)
from apps.notifications.models import Notification, NotificationKind
from apps.portal import services
from apps.portal.authentication import PortalJWTAuthentication
from apps.portal.models import PortalAccount, PortalRefreshSession
from apps.properties.models import Building, BuildingType
from apps.users import services as user_services
from core.context import company_context, get_current_company_id
from core.error_codes import ErrorCode
from tests.test_attachments import FakeS3
from tests.test_faults import Shop, api_for

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "staple-battery-horse"
TODAY = date.today()

#: Written into every internal field of the fixture. None may reach a contact.
SECRETS = (
    "INTERNAL-WORK-NOTE",
    "INTERNAL-STATUS-NOTE",
    "INTERNAL-ITEM-NOTE",
    "INTERNAL-ELEVATOR-NOTE",
    "BRAND-X-SECRET",
    "PARTS-USED-SECRET",
    "CANCEL-REASON-SECRET",
    "PUBLIC-REPORTER-NAME",
    "05550000000",
)
#: Keys that must never appear in a portal answer.
FORBIDDEN_KEYS = {
    "work_notes",
    "status_note",
    "note",
    "notes",
    "billable",
    "billable_overridden",
    "monthly_fee",
    "vat_rate",
    "brand",
    "model",
    "serial_number",
    "capacity_kg",
    "parts_used",
    "cancel_reason",
    "reporter_name",
    "reporter_phone",
    "technician",
    "technician_name",
    "reported_by",
    "last_name",
    "company_id",
    "contract",
}


def portal_for(account: PortalAccount) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {services.issue_tokens(account).access}")
    return client


class Site:
    """A customer of the shop with a lift and one of everything a contact reads."""

    def __init__(self, shop: Shop, *, customer: Customer, elevator: Elevator, tag: str) -> None:
        self.shop, self.customer, self.elevator, self.tag = shop, customer, elevator, tag
        company = shop.company
        with company_context(company.id):
            Elevator.objects.filter(pk=elevator.pk).update(
                notes="INTERNAL-ELEVATOR-NOTE", brand="BRAND-X-SECRET"
            )
            self.contact = CustomerContact.objects.create(
                company=company,
                customer=customer,
                full_name=f"Yonetici {tag}",
                email=f"manager-{tag}@example.com",
            )
            contract = Contract.objects.filter(lines__elevator=elevator).first() or shop.contract
            self.visit = MaintenanceVisit.objects.create(
                company=company,
                elevator=elevator,
                contract=contract,
                technician=shop.tech,
                visit_date=TODAY - timedelta(days=3),
                status=VisitStatus.COMPLETED,
                work_notes="INTERNAL-WORK-NOTE",
                status_note="INTERNAL-STATUS-NOTE",
                parts_replaced="Kapi kilidi",
            )
            MaintenanceVisitItem.objects.create(
                company=company,
                visit=self.visit,
                item=ChecklistItem.BRAKE_SYSTEM,
                result=ChecklistResult.OK,
                note="INTERNAL-ITEM-NOTE",
            )
            self.inspection = Inspection.objects.create(
                company=company,
                elevator=elevator,
                inspected_on=TODAY - timedelta(days=10),
                label=Label.YELLOW,
                inspection_body="TSE",
                report_number=f"R-{tag}",
                next_inspection_due=TODAY + timedelta(days=355),
                correction_deadline=TODAY + timedelta(days=110),
            )
            NonConformity.objects.create(
                company=company,
                inspection=self.inspection,
                description="Pit kirli",
                severity=Label.YELLOW,
            )
            self.fault = Fault.objects.create(
                company=company,
                elevator=elevator,
                reference=f"F{tag[:5].upper():0<5}",
                channel=FaultChannel.PUBLIC_QR,
                description="Kapi kapanmiyor",
                reporter_name="PUBLIC-REPORTER-NAME",
                reporter_phone="05550000000",
                technician=shop.tech,
                billable=True,
                parts_used="PARTS-USED-SECRET",
                cancel_reason="CANCEL-REASON-SECRET",
            )
            self.photo = self._file(ObjectType.MAINTENANCE_VISIT, self.visit.pk, "photo")
            self.report = self._file(ObjectType.INSPECTION, self.inspection.pk, "inspection_report")
            self.certificate = self._file(ObjectType.ELEVATOR, elevator.pk, "ce_certificate")
            self.contract_pdf = self._file(ObjectType.CONTRACT, contract.pk, "signed_contract")
        Inspection.unscoped.filter(pk=self.inspection.pk).update(report=self.report)
        self.account, _ = self.invite_and_accept()

    def _file(self, object_type: str, object_id: Any, category: str) -> Attachment:
        return Attachment.objects.create(
            company=self.shop.company,
            object_type=object_type,
            object_id=object_id,
            category=category,
            original_filename=f"{category}.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            storage_key=f"{self.shop.company.id}/{object_type}/{object_id}/{category}/x.pdf",
        )

    def invite_and_accept(self) -> tuple[PortalAccount, str]:
        with company_context(self.shop.company.id):
            _, token = services.invite(customer=self.customer, contact=self.contact)
        return services.accept(token=token, password=PASSWORD), token


def second_customer(shop: Shop, tag: str) -> tuple[Customer, Elevator]:
    with company_context(shop.company.id):
        customer = Customer.objects.create(
            company=shop.company, type=CustomerType.CORPORATE, legal_name=f"Musteri {tag}"
        )
        building = Building.objects.create(
            company=shop.company,
            customer=customer,
            name=f"Blok {tag}",
            type=BuildingType.RESIDENTIAL,
            address_note="x",
        )
        elevator = Elevator(
            company=shop.company, building=building, name=f"Lift {tag}", registration_number=tag
        )
        assign_qr_token(elevator)
        elevator.status = ElevatorStatus.ACTIVE
        elevator.save()
    return customer, elevator


@pytest.fixture
def world(db) -> dict[str, Any]:
    """Firm A with two customers, firm B with one. Every one fully stocked."""
    shop = Shop()
    ours = Site(shop, customer=shop.customer, elevator=shop.elevator, tag="ours")
    neighbour_customer, neighbour_lift = second_customer(shop, "neighbour")
    neighbour = Site(shop, customer=neighbour_customer, elevator=neighbour_lift, tag="neighbour")
    other_shop = Shop("B")
    stranger = Site(
        other_shop, customer=other_shop.customer, elevator=other_shop.elevator, tag="stranger"
    )
    django_mail.outbox.clear()
    return {"shop": shop, "ours": ours, "neighbour": neighbour, "stranger": stranger}


@pytest.fixture
def bucket(monkeypatch) -> FakeS3:
    from core import storage

    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda backend: fake)
    return fake


@pytest.fixture
def sent(django_capture_on_commit_callbacks):
    """Run the after-commit mail sends a request queues, as a real commit would."""

    def run(call: Any) -> Any:
        with django_capture_on_commit_callbacks(execute=True):
            return call()

    return run


READS = ["portal-elevator", "portal-visit", "portal-inspection", "portal-fault"]


def record_of(site: Site, basename: str) -> Any:
    return {
        "portal-elevator": site.elevator,
        "portal-visit": site.visit,
        "portal-inspection": site.inspection,
        "portal-fault": site.fault,
    }[basename]


# ---------------------------------------------------------------------------
# Tokens never cross
# ---------------------------------------------------------------------------


def _routes() -> list[tuple[str, type, list[str]]]:
    """(path, view class, methods) for every DRF route, with ids filled in."""
    found: list[tuple[str, type, list[str]]] = []

    def walk(patterns: Any, prefix: str) -> None:
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, prefix + str(entry.pattern))
                continue
            callback = entry.callback
            cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if cls is None:
                continue
            raw = prefix + str(entry.pattern)
            if "format" in raw:
                continue
            actions = getattr(callback, "actions", None)
            if actions:
                methods = sorted(actions)
            else:
                methods = [m for m in ("get", "post", "put", "patch", "delete") if hasattr(cls, m)]
            path = re.sub(r"\(\?P<\w+>[^)]*\)", str(uuid.uuid4()), raw)
            path = re.sub(r"<(?:\w+:)?\w+>", str(uuid.uuid4()), path)
            path = "/" + path.replace("^", "").replace("$", "")
            if not path.startswith("/api/"):
                continue
            found.append((path, cls, methods))

    walk(get_resolver().url_patterns, "")
    return found


def _is_portal(cls: type) -> bool:
    return PortalJWTAuthentication in getattr(cls, "authentication_classes", ())


def _is_open(cls: type) -> bool:
    return AllowAny in getattr(cls, "permission_classes", ())


PORTAL_ROUTES = [(p, c, m) for p, c, m in _routes() if _is_portal(c)]
STAFF_ROUTES = [(p, c, m) for p, c, m in _routes() if not _is_portal(c) and not _is_open(c)]


class TestTokensNeverCross:
    def test_the_sweeps_found_routes(self):
        assert len(PORTAL_ROUTES) >= 8
        assert len(STAFF_ROUTES) >= 40

    @pytest.mark.parametrize(("path", "cls", "methods"), PORTAL_ROUTES)
    def test_a_staff_token_opens_no_portal_endpoint(self, world, path, cls, methods):
        client = api_for(world["shop"].owner)
        for method in methods:
            response = getattr(client, method)(path, {}, format="json")
            assert response.status_code == 401, f"{method.upper()} {path}: {response.status_code}"

    @pytest.mark.parametrize(("path", "cls", "methods"), STAFF_ROUTES)
    def test_a_portal_token_opens_no_staff_endpoint(self, world, path, cls, methods):
        client = portal_for(world["ours"].account)
        for method in methods:
            response = getattr(client, method)(path, {}, format="json")
            assert response.status_code == 401, f"{method.upper()} {path}: {response.status_code}"

    def test_the_portal_token_carries_nothing_staff_side_keys_on(self, world):
        from rest_framework_simplejwt.tokens import AccessToken

        raw = services.issue_tokens(world["ours"].account).access
        token = AccessToken(raw)  # type: ignore[arg-type]  # the stubs omit the str form
        assert token["aud"] == "portal"
        assert "user_id" not in token
        assert "company_id" not in token
        assert "role" not in token

    def test_the_staff_refresh_cookie_does_not_refresh_a_portal_session(self, world):
        client = APIClient()
        staff = user_services.issue_tokens(world["shop"].owner)
        client.cookies["shiftlush_refresh"] = staff.refresh
        response = client.post(reverse("portal-refresh"))
        assert response.status_code == 422
        client.cookies["portal_refresh"] = staff.refresh
        assert client.post(reverse("portal-refresh")).status_code == 422


# ---------------------------------------------------------------------------
# Customer scope
# ---------------------------------------------------------------------------


class TestCustomerScope:
    @pytest.mark.parametrize("basename", READS)
    def test_a_list_holds_only_the_customers_own(self, world, basename):
        client = portal_for(world["ours"].account)
        rows = client.get(reverse(f"{basename}-list")).data["results"]
        assert [str(row["id"]) for row in rows] == [str(record_of(world["ours"], basename).pk)]

    @pytest.mark.parametrize("basename", READS)
    @pytest.mark.parametrize("other", ["neighbour", "stranger"])
    def test_another_customers_record_is_not_found(self, world, basename, other):
        client = portal_for(world["ours"].account)
        url = reverse(f"{basename}-detail", args=[record_of(world[other], basename).pk])
        response = client.get(url)
        assert response.status_code == 404
        assert response.data["error"]["code"] == ErrorCode.NOT_FOUND.value

    def test_a_fault_cannot_be_reported_on_a_neighbours_lift(self, world):
        response = portal_for(world["ours"].account).post(
            reverse("portal-fault-list"),
            {"elevator": str(world["neighbour"].elevator.pk), "description": "x"},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "elevator", "code": "does_not_exist"}
        ]

    @pytest.mark.parametrize("which", ["photo", "report"])
    def test_own_files_download(self, world, which, bucket):
        attachment = getattr(world["ours"], which)
        response = portal_for(world["ours"].account).get(
            reverse("portal-file-download-url", args=[attachment.pk])
        )
        assert response.status_code == 200, response.data
        assert response.data["download_url"].startswith("https://")

    @pytest.mark.parametrize(
        ("site", "which"),
        [
            ("neighbour", "photo"),
            ("neighbour", "report"),
            ("stranger", "photo"),
            # Their own customer's, but not portal material.
            ("ours", "certificate"),
            ("ours", "contract_pdf"),
        ],
    )
    def test_other_files_are_not_found(self, world, site, which):
        attachment = getattr(world[site], which)
        response = portal_for(world["ours"].account).get(
            reverse("portal-file-download-url", args=[attachment.pk])
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Field exposure
# ---------------------------------------------------------------------------


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for item in value for k in _keys(item)}
    return set()


class TestFieldExposure:
    def test_nothing_internal_reaches_a_contact(self, world):
        client = portal_for(world["ours"].account)
        bodies = [client.get(reverse("portal-me")).data]
        for basename in READS:
            bodies.append(client.get(reverse(f"{basename}-list")).data)
            record = record_of(world["ours"], basename)
            bodies.append(client.get(reverse(f"{basename}-detail", args=[record.pk])).data)
        raw = json.dumps(bodies, default=str)

        for secret in SECRETS:
            assert secret not in raw, secret
        assert _keys(bodies) & FORBIDDEN_KEYS == set()
        # The technician by first name, never their full name.
        assert "Ali" in raw
        assert "Ali X" not in raw

    def test_what_a_visit_does_show(self, world):
        client = portal_for(world["ours"].account)
        visit = client.get(reverse("portal-visit-detail", args=[world["ours"].visit.pk])).data
        assert visit["technician_first_name"] == "Ali"
        assert visit["parts_replaced"] == "Kapi kilidi"
        assert visit["items"] == [{"item": "brake_system", "result": "ok"}]
        assert [str(p["id"]) for p in visit["photos"]] == [str(world["ours"].photo.pk)]

    def test_what_an_inspection_and_a_fault_show(self, world):
        client = portal_for(world["ours"].account)
        inspection = client.get(
            reverse("portal-inspection-detail", args=[world["ours"].inspection.pk])
        ).data
        assert inspection["label"] == Label.YELLOW
        assert inspection["report"] == world["ours"].report.pk
        assert inspection["nonconformities"][0]["status"] == "open"
        fault = client.get(reverse("portal-fault-detail", args=[world["ours"].fault.pk])).data
        assert fault["technician_first_name"] == "Ali"
        assert fault["reported_by_me"] is False

    def test_me_says_whom_to_call(self, world):
        Customer.unscoped.filter(pk=world["ours"].customer.pk).update(legal_name="Site Yonetimi")
        from apps.companies.models import Company

        Company.unscoped.filter(pk=world["shop"].company.pk).update(phone="02120000000")
        me = portal_for(world["ours"].account).get(reverse("portal-me")).data
        assert me == {
            "id": str(world["ours"].account.pk),
            "email": "manager-ours@example.com",
            "contact_name": "Yonetici ours",
            "customer_name": "Site Yonetimi",
            "company_name": "Shop ",
            "company_phone": "02120000000",
        }


# ---------------------------------------------------------------------------
# The account's life
# ---------------------------------------------------------------------------


def invite(shop: Shop, customer: Customer, contact: CustomerContact, by: Any = None) -> Any:
    return api_for(by or shop.owner).post(
        reverse("customer-portal-invitations", args=[customer.pk]),
        {"contact": str(contact.pk)},
        format="json",
    )


def new_contact(shop: Shop, email: str = "new@example.com") -> CustomerContact:
    with company_context(shop.company.id):
        return CustomerContact.objects.create(
            company=shop.company, customer=shop.customer, full_name="Yeni Kisi", email=email
        )


def token_from(message: Any) -> str:
    match = re.search(r"/portal/(?:accept|password-reset)/([\w-]+)", message.body)
    assert match, message.body
    return match.group(1)


def login(email: str, password: str = PASSWORD, address: str = "198.51.100.1") -> Any:
    return APIClient().post(
        reverse("portal-login"),
        {"email": email, "password": password},
        format="json",
        REMOTE_ADDR=address,
    )


class TestInvitation:
    def test_invite_then_accept(self, world, sent):
        shop = world["shop"]
        contact = new_contact(shop)
        response = sent(lambda: invite(shop, shop.customer, contact))
        assert response.status_code == 201, response.data
        assert response.data["status"] == "invited"
        (message,) = django_mail.outbox
        assert message.to == ["new@example.com"]
        assert "portal" in message.subject.lower()
        assert "müşteri portalı" in message.subject
        assert "/portal/accept/" in message.body

        accepted = APIClient().post(
            reverse("portal-accept"),
            {"token": token_from(message), "password": PASSWORD},
            format="json",
        )
        assert accepted.status_code == 201, accepted.data
        assert accepted.data["account"]["email"] == "new@example.com"
        cookie = accepted.cookies["portal_refresh"]
        assert cookie["path"] == "/api/v1/portal/"
        assert cookie["httponly"]
        # Single use.
        again = APIClient().post(
            reverse("portal-accept"),
            {"token": token_from(message), "password": PASSWORD},
            format="json",
        )
        assert again.data["error"]["code"] == ErrorCode.TOKEN_INVALID.value

    def test_a_contact_without_email_cannot_be_invited(self, world):
        shop = world["shop"]
        contact = new_contact(shop, email="")
        response = invite(shop, shop.customer, contact)
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.CONTACT_HAS_NO_EMAIL.value

    def test_the_contact_must_be_the_customers(self, world):
        shop = world["shop"]
        response = invite(shop, shop.customer, world["neighbour"].contact)
        assert response.data["error"]["code"] == ErrorCode.CONTACT_NOT_OF_CUSTOMER.value
        foreign = invite(shop, shop.customer, world["stranger"].contact)
        assert foreign.data["error"]["code"] == ErrorCode.CONTACT_NOT_OF_CUSTOMER.value

    def test_an_active_account_is_not_invited_again(self, world):
        response = invite(world["shop"], world["shop"].customer, world["ours"].contact)
        assert response.status_code == 409
        assert response.data["error"]["code"] == ErrorCode.PORTAL_ACCOUNT_ALREADY_ACTIVE.value

    def test_inviting_again_retires_the_first_link(self, world, sent):
        shop = world["shop"]
        contact = new_contact(shop)
        sent(lambda: invite(shop, shop.customer, contact))
        sent(lambda: invite(shop, shop.customer, contact))
        first, second = django_mail.outbox
        stale = APIClient().post(
            reverse("portal-accept"), {"token": token_from(first), "password": PASSWORD}
        )
        assert stale.data["error"]["code"] == ErrorCode.TOKEN_INVALID.value
        fresh = APIClient().post(
            reverse("portal-accept"), {"token": token_from(second), "password": PASSWORD}
        )
        assert fresh.status_code == 201

    @pytest.mark.parametrize("role", ["tech", "accountant"])
    def test_office_roles_only(self, world, role):
        shop = world["shop"]
        user = getattr(shop, role)
        contact = new_contact(shop)
        assert invite(shop, shop.customer, contact, by=user).status_code == 403
        listed = api_for(user).get(reverse("customer-portal-accounts", args=[shop.customer.pk]))
        assert listed.status_code == 403
        deactivate = api_for(user).post(
            reverse("portal-account-deactivate", args=[world["ours"].account.pk])
        )
        assert deactivate.status_code == 403

    def test_the_office_lists_and_closes_accounts(self, world):
        shop = world["shop"]
        client = api_for(shop.ops)
        rows = client.get(reverse("customer-portal-accounts", args=[shop.customer.pk])).data
        assert [row["email"] for row in rows["results"]] == ["manager-ours@example.com"]
        assert rows["results"][0]["status"] == "active"
        access = portal_for(world["ours"].account)

        closed = client.post(reverse("portal-account-deactivate", args=[world["ours"].account.pk]))
        assert closed.status_code == 200
        assert closed.data["status"] == "inactive"
        assert access.get(reverse("portal-me")).status_code == 401
        assert login("manager-ours@example.com").data["error"]["code"] == (
            ErrorCode.ACCOUNT_INACTIVE.value
        )

    def test_another_firms_customer_and_account_are_not_found(self, world):
        client = api_for(world["shop"].owner)
        stranger = world["stranger"]
        listed = client.get(reverse("customer-portal-accounts", args=[stranger.customer.pk]))
        assert listed.status_code == 404
        closed = client.post(reverse("portal-account-deactivate", args=[stranger.account.pk]))
        assert closed.status_code == 404
        assert PortalAccount.unscoped.get(pk=stranger.account.pk).is_active

    def test_a_deleted_contact_closes_the_account(self, world):
        ours = world["ours"]
        access = portal_for(ours.account)
        response = api_for(world["shop"].owner).delete(
            reverse("customer-contact-detail", args=[ours.contact.pk])
        )
        assert response.status_code == 204, response.data
        assert PortalAccount.unscoped.get(pk=ours.account.pk).is_active is False
        assert access.get(reverse("portal-me")).status_code == 401


class TestSignIn:
    def test_login_sets_the_portal_cookie(self, world):
        response = login("manager-ours@example.com")
        assert response.status_code == 200, response.data
        assert response.data["account"]["customer_name"] == world["ours"].customer.legal_name
        assert response.cookies["portal_refresh"]["path"] == "/api/v1/portal/"
        assert "shiftlush_refresh" not in response.cookies
        me = APIClient()
        me.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        assert me.get(reverse("portal-me")).status_code == 200

    def test_a_wrong_password_and_an_unknown_address_read_the_same(self, world):
        wrong = login("manager-ours@example.com", "nope-nope-nope")
        unknown = login("nobody@example.com")
        assert wrong.data["error"]["code"] == unknown.data["error"]["code"]
        assert wrong.data["error"]["code"] == ErrorCode.INVALID_CREDENTIALS.value

    def test_an_invited_contact_cannot_sign_in_before_accepting(self, world):
        shop = world["shop"]
        invite(shop, shop.customer, new_contact(shop))
        assert login("new@example.com").data["error"]["code"] == (
            ErrorCode.INVALID_CREDENTIALS.value
        )

    def test_five_failures_lock_the_pair(self, world):
        for _ in range(5):
            login("manager-ours@example.com", "nope-nope-nope")
        locked = login("manager-ours@example.com")
        assert locked.data["error"]["code"] == ErrorCode.ACCOUNT_LOCKED.value
        # Another address is a different bucket, as for staff.
        assert login("manager-ours@example.com", address="198.51.100.2").status_code == 200

    def test_portal_failures_do_not_lock_a_staff_sign_in(self, world):
        owner_email = world["shop"].owner.email
        for _ in range(6):
            login(owner_email, "nope-nope-nope")
        staff = APIClient().post(
            reverse("auth:login"),
            {"email": owner_email, "password": PASSWORD},
            format="json",
            REMOTE_ADDR="198.51.100.1",
        )
        assert staff.status_code == 200

    def test_refresh_rotates_and_a_replay_ends_every_session(self, world):
        first = login("manager-ours@example.com")
        old = first.cookies["portal_refresh"].value
        client = APIClient()
        client.cookies["portal_refresh"] = old
        rotated = client.post(reverse("portal-refresh"))
        assert rotated.status_code == 200
        new = rotated.cookies["portal_refresh"].value
        assert new != old

        replay = APIClient()
        replay.cookies["portal_refresh"] = old
        assert replay.post(reverse("portal-refresh")).status_code == 422
        assert not PortalRefreshSession.objects.filter(
            account=world["ours"].account, revoked_at__isnull=True
        ).exists()

    def test_logout_ends_the_session(self, world):
        token = login("manager-ours@example.com").cookies["portal_refresh"].value
        client = APIClient()
        client.cookies["portal_refresh"] = token
        out = client.post(reverse("portal-logout"))
        assert out.status_code == 204
        again = APIClient()
        again.cookies["portal_refresh"] = token
        assert again.post(reverse("portal-refresh")).status_code == 422

    def test_password_reset(self, world, sent):
        login("manager-ours@example.com")
        requested = sent(
            lambda: APIClient().post(
                reverse("portal-password-reset"), {"email": "manager-ours@example.com"}
            )
        )
        assert requested.status_code == 202
        unknown = APIClient().post(reverse("portal-password-reset"), {"email": "x@example.com"})
        assert unknown.status_code == 202
        (message,) = django_mail.outbox
        assert "/portal/password-reset/" in message.body

        confirmed = APIClient().post(
            reverse("portal-password-reset-confirm"),
            {"token": token_from(message), "password": NEW_PASSWORD},
        )
        assert confirmed.status_code == 204
        assert login("manager-ours@example.com", NEW_PASSWORD).status_code == 200
        assert login("manager-ours@example.com").status_code != 200

    def test_a_staff_reset_link_does_nothing_here(self, world):
        token = user_services.request_password_reset(world["shop"].owner.email)
        assert token
        response = APIClient().post(
            reverse("portal-password-reset-confirm"), {"token": token, "password": NEW_PASSWORD}
        )
        assert response.data["error"]["code"] == ErrorCode.TOKEN_INVALID.value


# ---------------------------------------------------------------------------
# Reporting a fault
# ---------------------------------------------------------------------------


def report(site: Site, key: str | None = None, **body: Any) -> Any:
    headers: dict[str, Any] = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return portal_for(site.account).post(
        reverse("portal-fault-list"),
        {"elevator": str(site.elevator.pk), "description": "Kabin sarsiliyor", **body},
        format="json",
        **headers,
    )


class TestReportingAFault:
    def test_reported_on_the_customers_lift(self, world):
        response = report(world["ours"])
        assert response.status_code == 201, response.data
        assert response.data["channel"] == FaultChannel.CUSTOMER_PORTAL
        assert response.data["reported_by_me"] is True
        fault = Fault.unscoped.get(pk=response.data["id"])
        assert fault.reported_by_portal_account_id == world["ours"].account.pk
        assert fault.company_id == world["shop"].company.id
        shop = world["shop"]
        told = {
            n.recipient_id
            for n in Notification.unscoped.filter(kind=NotificationKind.FAULT_REPORTED)
        }
        assert {shop.owner.id, shop.ops.id, shop.tech.id} <= told

    @pytest.mark.parametrize(
        ("body", "priority"),
        [
            ({}, FaultPriority.NORMAL),
            ({"elevator_stopped": True}, FaultPriority.HIGH),
            ({"someone_trapped": True}, FaultPriority.EMERGENCY),
        ],
    )
    def test_the_public_priority_mapping(self, world, body, priority):
        response = report(world["ours"], **body)
        assert response.data["priority"] == priority

    def test_a_trapped_passenger_is_an_emergency_notice(self, world):
        report(world["ours"], someone_trapped=True)
        assert Notification.unscoped.filter(kind=NotificationKind.FAULT_EMERGENCY).exists()

    def test_no_merge_two_reports_are_two_faults(self, world):
        report(world["ours"])
        report(world["ours"])
        assert Fault.unscoped.filter(channel=FaultChannel.CUSTOMER_PORTAL).count() == 2

    def test_a_retry_with_the_same_key_is_one_fault(self, world):
        first = report(world["ours"], key="portal-retry-1")
        second = report(world["ours"], key="portal-retry-1")
        assert second.status_code == 201
        assert second.data["id"] == first.data["id"]
        assert Fault.unscoped.filter(channel=FaultChannel.CUSTOMER_PORTAL).count() == 1

    def test_priority_is_not_settable_directly(self, world):
        response = report(world["ours"], priority="emergency")
        assert response.status_code == 400

    def test_the_new_fault_is_in_the_list(self, world):
        created = report(world["ours"]).data
        rows = portal_for(world["ours"].account).get(reverse("portal-fault-list")).data
        assert created["id"] in {row["id"] for row in rows["results"]}


class TestThePortalLeavesNoFirmBehind:
    """`PortalViewSet` binds the firm context in `initial` and releases it in
    `finalize_response`. A context left bound would be inherited by whatever
    request the same worker thread serves next — a staff request included."""

    def test_after_a_normal_answer(self, world):
        portal_for(world["ours"].account).get(reverse("portal-elevator-list"))
        assert get_current_company_id() is None

    def test_after_a_not_found(self, world):
        portal_for(world["ours"].account).get(
            reverse("portal-elevator-detail", args=[world["stranger"].elevator.pk])
        )
        assert get_current_company_id() is None

    def test_after_an_unexpected_error(self, world):
        from apps.portal.api.v1 import views as portal_views

        client = portal_for(world["ours"].account)
        client.raise_request_exception = False
        with mock.patch.object(
            portal_views.PortalElevatorViewSet, "list", side_effect=RuntimeError("boom")
        ):
            response = client.get(reverse("portal-elevator-list"))

        assert response.status_code == 500
        assert get_current_company_id() is None
