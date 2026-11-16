"""Scope and field work follow the role's flags, not the technician role (A4).

`assigned_only` decides what a user sees and that they act on their own work
only; `field_staff` decides who can be given customers, sent to faults, listed
as a technician, told about a customer's faults, and who photographs their own
work without `attachment` write. The seeded technician has both, and the
existing suites cover it; every role here is one a company made itself, so the
seed key (`user.role`) is empty and only the flags can be what decides.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.customers.models import Customer
from apps.faults.models import Fault, FaultChannel
from apps.inspections.models import Inspection, Label
from apps.maintenance.models import MaintenanceVisit, PlannedVisit, VisitStatus
from apps.notifications.models import NotificationKind
from apps.users import services
from apps.users.models import CompanyRole, Role, RoleGrant, User
from apps.users.services import issue_tokens
from core.context import company_context, system_context
from core.error_codes import ErrorCode
from core.permission_catalogue import READ, WRITE
from tests.test_faults import PASSWORD, Shop, told

TODAY = date.today()

#: What a field worker reads and does, short of the attachment write the
#: own-work carve-out stands in for.
FIELD_GRANTS: dict[str, tuple[str, ...]] = {
    "customer": (READ,),
    "building": (READ,),
    "elevator": (READ,),
    "maintenance_visit": (READ, WRITE),
    "planned_visit": (READ, WRITE),
    "fault": (READ, WRITE),
    "inspection": (READ,),
    "attachment": (READ,),
}


def custom_role(
    shop: Shop,
    name: str,
    *,
    assigned_only: bool,
    field_staff: bool,
    grants: dict[str, tuple[str, ...]] = FIELD_GRANTS,
) -> CompanyRole:
    with system_context():
        role = CompanyRole.unscoped.create(
            company=shop.company, name=name, assigned_only=assigned_only, field_staff=field_staff
        )
        RoleGrant.objects.bulk_create(
            RoleGrant(
                role=role,
                resource=resource,
                can_read=READ in actions,
                can_write=WRITE in actions,
            )
            for resource, actions in grants.items()
        )
    return role


def member(shop: Shop, role: CompanyRole, email: str, *customers: Customer) -> User:
    with system_context():
        user = User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=shop.company,
            first_name=email.split("@")[0].title(),
            last_name="X",
            company_role=role,
            is_email_verified=True,
        )
    with company_context(shop.company.id):
        for customer in customers:
            user.customer_assignments.create(company=shop.company, customer=customer)
    return user


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    return client


def other_customer(shop: Shop) -> Customer:
    with company_context(shop.company.id):
        return Customer.objects.get(legal_name="Other ")


@pytest.fixture
def shop(db) -> Shop:
    return Shop()


@pytest.fixture
def work(shop: Shop) -> dict[str, Any]:
    """One of everything a scoped role is narrowed on, all at the shop's customer."""
    with company_context(shop.company.id):
        visit = MaintenanceVisit.objects.create(
            company=shop.company,
            elevator=shop.elevator,
            contract=shop.contract,
            technician=shop.tech,
            visit_date=TODAY,
            status=VisitStatus.COMPLETED,
        )
        fault = Fault.objects.create(
            company=shop.company,
            elevator=shop.elevator,
            reference="FLT001",
            channel=FaultChannel.STAFF,
            reported_by=shop.owner,
            description="Kapı kapanmıyor",
        )
        inspection = Inspection.objects.create(
            company=shop.company,
            elevator=shop.elevator,
            inspected_on=TODAY,
            label=Label.GREEN,
            next_inspection_due=TODAY + timedelta(days=365),
        )
        planned = PlannedVisit.objects.create(
            company=shop.company, elevator=shop.elevator, period=f"{TODAY:%Y-%m}", due_date=TODAY
        )
    return {
        "customer": shop.customer,
        "building": shop.building,
        "elevator": shop.elevator,
        "visit": visit,
        "fault": fault,
        "inspection": inspection,
        "planned": planned,
    }


#: Each list a scoped role is narrowed on, and which row of `work` it shows.
NARROWED = {
    "customer-list": "customer",
    "building-list": "building",
    "elevator-list": "elevator",
    "maintenance-visit-list": "visit",
    "fault-list": "fault",
    "inspection-list": "inspection",
    "planned-visit-list": "planned",
}


def listed(user: User, route: str, **params: str) -> set[str]:
    response = api_for(user).get(reverse(route), params)
    assert response.status_code == 200, response.data
    return {str(row["id"]) for row in response.data["results"]}


class TestTheFlagsOnTheUser:
    def test_follow_the_role(self, shop):
        scoped = member(
            shop, custom_role(shop, "Saha", assigned_only=True, field_staff=True), "s@example.com"
        )
        field = member(
            shop, custom_role(shop, "Usta", assigned_only=False, field_staff=True), "f@example.com"
        )
        office = member(
            shop, custom_role(shop, "Ofis", assigned_only=False, field_staff=False), "o@example.com"
        )
        assert (scoped.is_scoped, scoped.is_field_staff) == (True, True)
        assert (field.is_scoped, field.is_field_staff) == (False, True)
        assert (office.is_scoped, office.is_field_staff) == (False, False)

    def test_no_role_is_neither(self, db):
        with system_context():
            root = User.objects.create_superuser(
                email="root@example.com", password=PASSWORD, first_name="R", last_name="T"
            )
        assert root.company_role is None
        assert (root.is_scoped, root.is_field_staff) == (False, False)


class TestAScopedRole:
    @pytest.mark.parametrize(("route", "row"), NARROWED.items())
    def test_a_custom_scoped_role_sees_only_assigned_customers(self, shop, work, route, row):
        role = custom_role(shop, "Saha", assigned_only=True, field_staff=True)
        assigned = member(shop, role, "here@example.com", shop.customer)
        elsewhere = member(shop, role, "there@example.com", other_customer(shop))
        expected = str(work[row].id)
        params = {"period": f"{TODAY:%Y-%m}"} if route == "planned-visit-list" else {}

        assert expected in listed(assigned, route, **params)
        assert expected not in listed(elsewhere, route, **params)

    def test_an_unscoped_role_with_the_same_grants_sees_the_whole_firm(self, shop, work):
        role = custom_role(shop, "Usta", assigned_only=False, field_staff=True)
        roaming = member(shop, role, "roam@example.com")
        assert str(work["fault"].id) in listed(roaming, "fault-list")
        assert str(shop.customer.id) in listed(roaming, "customer-list")

    def test_works_a_fault_only_when_sent_to_it(self, shop, work):
        role = custom_role(shop, "Saha", assigned_only=True, field_staff=True)
        scoped = member(shop, role, "here@example.com", shop.customer)
        assert shop.act(work["fault"].id, "assign", technician=str(shop.tech.id)).status_code == 200

        # Visible (the customer is theirs), but not their fault to work.
        assert shop.act(work["fault"].id, "arrive", by=scoped).status_code == 403

    def test_names_only_themselves_on_a_fault_they_report(self, shop):
        role = custom_role(shop, "Saha", assigned_only=True, field_staff=True)
        scoped = member(shop, role, "here@example.com", shop.customer)
        response = api_for(scoped).post(
            reverse("fault-list"),
            {
                "elevator": str(shop.elevator.id),
                "description": "x",
                "technician": str(shop.tech.id),
            },
            format="json",
        )
        assert response.status_code == 400, response.data
        assert [d["field"] for d in response.data["error"]["details"]] == ["technician"]

    def test_records_a_visit_in_their_own_name(self, shop):
        role = custom_role(shop, "Saha", assigned_only=True, field_staff=True)
        scoped = member(shop, role, "here@example.com", shop.customer)
        body = {
            "elevator": str(shop.elevator.id),
            "contract": str(shop.contract.id),
            "visit_date": str(TODAY),
            "status": VisitStatus.COMPLETED,
            "signature_status": "not_present",
        }

        mine = api_for(scoped).post(reverse("maintenance-visit-list"), body)
        theirs = api_for(scoped).post(
            reverse("maintenance-visit-list"), {**body, "technician": str(shop.tech.id)}
        )

        assert mine.status_code == 201, mine.data
        assert mine.data["technician"] == scoped.id
        assert theirs.status_code == 400
        assert theirs.data["error"]["details"] == [
            {"code": ErrorCode.TECHNICIAN_RECORDS_OWN_VISITS, "field": "technician"}
        ]


class TestFieldStaff:
    def test_an_unscoped_field_staff_role_sees_every_fault_and_can_be_assigned_one(
        self, shop, work
    ):
        role = custom_role(shop, "Usta", assigned_only=False, field_staff=True)
        roaming = member(shop, role, "roam@example.com")
        fault_id = work["fault"].id

        assert str(fault_id) in listed(roaming, "fault-list")
        assert shop.act(fault_id, "assign", technician=str(roaming.id)).status_code == 200
        assert shop.act(fault_id, "arrive", by=roaming).status_code == 200

    def test_are_the_technicians_a_visit_can_name(self, shop):
        field = member(
            shop, custom_role(shop, "Usta", assigned_only=False, field_staff=True), "f@example.com"
        )
        office = member(
            shop, custom_role(shop, "Ofis", assigned_only=False, field_staff=False), "o@example.com"
        )
        response = api_for(shop.owner).get(reverse("maintenance-visit-technicians"))
        ids = {str(row["id"]) for row in response.data}
        assert str(field.id) in ids
        assert str(office.id) not in ids
        assert str(shop.tech.id) in ids

    def test_hear_about_a_fault_at_their_customer(self, shop):
        field = member(
            shop,
            custom_role(shop, "Usta", assigned_only=False, field_staff=True),
            "f@example.com",
            shop.customer,
        )
        shop.report(by=shop.ops)
        assert field.email in told(NotificationKind.FAULT_REPORTED)

    def test_a_field_staff_user_uploads_to_their_own_visit_without_attachment_write(
        self, shop, work
    ):
        role = custom_role(shop, "Usta", assigned_only=False, field_staff=True)
        field = member(shop, role, "f@example.com", shop.customer)
        office_role = custom_role(shop, "Ofis", assigned_only=False, field_staff=False)
        office = member(shop, office_role, "o@example.com", shop.customer)
        body = {
            "object_type": ObjectType.MAINTENANCE_VISIT,
            "object_id": str(work["visit"].id),
            "category": AttachmentCategory.PHOTO,
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        }

        assert api_for(field).post(reverse("attachment-upload-url"), body).status_code == 200
        # The same grants and the same customer, but not field staff: no carve-out.
        assert api_for(office).post(reverse("attachment-upload-url"), body).status_code == 403


class TestCustomerAssignments:
    def put(self, shop: Shop, user: User, *customers: Customer) -> Any:
        return api_for(shop.owner).put(
            reverse("user-customers", args=[user.id]),
            {"customer_ids": [str(c.id) for c in customers]},
            format="json",
        )

    def test_a_field_staff_role_receives_customer_assignments(self, shop):
        field = member(
            shop, custom_role(shop, "Usta", assigned_only=False, field_staff=True), "f@example.com"
        )
        response = self.put(shop, field, shop.customer)
        assert response.status_code == 200, response.data
        assert response.data["assigned_customer_ids"] == [str(shop.customer.id)]

    def test_a_non_field_staff_role_cannot_receive_customer_assignments(self, shop):
        office = member(
            shop, custom_role(shop, "Ofis", assigned_only=False, field_staff=False), "o@example.com"
        )
        response = self.put(shop, office, shop.customer)
        assert response.status_code == 422
        assert response.data["error"]["code"] == ErrorCode.ONLY_FIELD_STAFF_ARE_ASSIGNED

    def test_moving_a_user_to_a_non_field_staff_role_clears_their_assignments(self, shop):
        field = member(
            shop,
            custom_role(shop, "Usta", assigned_only=False, field_staff=True),
            "f@example.com",
            shop.customer,
        )
        with company_context(shop.company.id):
            services.change_role(user=field, role=Role.ACCOUNTANT)
            assert not field.customer_assignments.exists()

    def test_moving_a_technician_to_a_role_that_is_field_staff_keeps_them(self, shop):
        # A firm that sends its operations people out too.
        CompanyRole.unscoped.filter(company=shop.company, system_key=Role.OPERATIONS).update(
            field_staff=True
        )
        with company_context(shop.company.id):
            services.change_role(user=shop.tech, role=Role.OPERATIONS)
            assert shop.tech.customer_assignments.filter(customer=shop.customer).exists()
