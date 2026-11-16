"""Who hears about it is a permission, not a role name (A5).

Each of the six notice sites addresses `holders(company, "notify_...", RECEIVE)`.
For the seeded roles that must be exactly the people each site used to name, and
a custom role is addressed the moment it is granted the notice.
"""

# ruff: noqa: F811
# (the imported fixtures are requested by name, which reads as a redefinition)
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from apps.billing.services import _billing_people
from apps.companies.models import Company
from apps.contracts.tasks import contract_renewal_notice
from apps.faults.services import _office as fault_office
from apps.inspections.services import office as inspection_office
from apps.maintenance.tasks import maintenance_overdue
from apps.notifications.models import Notification, NotificationKind
from apps.users.models import CompanyRole, RoleGrant, User
from core.context import system_context
from core.permission_catalogue import RECEIVE
from core.permissions import holders

# The fixtures and builders the job tests already own; reused so the recipients
# are judged through the same real jobs.
from tests.test_customer_reports import (  # noqa: F401
    NEXT_MONTH,
    bucket,
    month,
    pdf,
    run_job,
    switch,
    tick,
)
from tests.test_lifecycle_jobs import (  # noqa: F401
    TODAY,
    contract_for,
    firm,
    notified,
    today,
)

PASSWORD = "x-Strong-pass-1"
ROLES = ("owner", "admin", "operations", "accountant", "technician")
OWNER_ADMIN = {"owner", "admin"}
OFFICE = {"owner", "admin", "operations"}


def add_role_users(company: Company) -> dict[str, User]:
    """One active user per seeded role, whose e-mail is the role key."""
    people: dict[str, User] = {}
    with system_context():
        for key in ROLES:
            email = f"{key}@recipients.test"
            people[key] = User.objects.filter(email=email).first() or User.objects.create_user(
                email=email,
                password=PASSWORD,
                company=company,
                first_name=key.title(),
                last_name="Person",
                role=key,
            )
    return people


def roles_of(users: Any) -> set[str]:
    return {u.email.split("@")[0] for u in users}


@pytest.fixture
def company(db) -> Company:
    with system_context():
        company = Company.objects.create(legal_name="Rcpt Ltd", display_name="Rcpt")
    add_role_users(company)
    return company


def test_holders_of_each_notice_are_the_legacy_role_sets(company):
    for resource, expected in {
        "notify_contract": OWNER_ADMIN,
        "notify_fault_office": OFFICE,
        "notify_inspection_office": OFFICE,
        "notify_report": OWNER_ADMIN,
        "notify_billing": OWNER_ADMIN | {"accountant"},
        "notify_plan": OFFICE,
    }.items():
        assert roles_of(holders(company.pk, resource, RECEIVE)) == expected, resource


def test_fault_office_recipients(company):
    assert roles_of(fault_office(company.pk)) == OFFICE


def test_inspection_office_recipients(company):
    assert roles_of(inspection_office(company.pk)) == OFFICE


def test_billing_recipients(company):
    assert roles_of(_billing_people(company.pk)) == OWNER_ADMIN | {"accountant"}


@pytest.mark.usefixtures("today")
def test_contract_notice_recipients(firm):
    contract_for(firm, end=TODAY + timedelta(days=30))
    contract_renewal_notice.call()
    assert roles_of_emails(notified(NotificationKind.CONTRACT_RENEWAL_DUE)) == OWNER_ADMIN


@pytest.mark.usefixtures("today")
def test_plan_overdue_office_recipients(firm):
    _, _, elevator, _ = firm
    contract_for(firm, end=TODAY + timedelta(days=200))
    from apps.elevators.models import Elevator

    Elevator.unscoped.filter(id=elevator.id).update(next_maintenance_date=TODAY - timedelta(days=3))
    maintenance_overdue.call()
    # The lift's own technician hears too, but through assignment, not the notice.
    assert roles_of_emails(notified(NotificationKind.MAINTENANCE_OVERDUE)) == OFFICE | {
        "technician"
    }


@pytest.mark.usefixtures("pdf", "bucket")
def test_monthly_report_recipients(month, monkeypatch):
    add_role_users(month.company)
    switch(month, True)
    tick(month, month.a)
    run_job(NEXT_MONTH, monkeypatch)
    told = Notification.unscoped.filter(kind=NotificationKind.MONTHLY_REPORTS_READY)
    heard = set(told.values_list("recipient__company_role__system_key", flat=True))
    assert heard == OWNER_ADMIN
    assert told.filter(recipient__email="operations@recipients.test").count() == 0


def roles_of_emails(emails: set[str]) -> set[str]:
    return {e.split("@")[0] for e in emails}


def custom_role(company: Company, resource: str | None, **flags: bool) -> CompanyRole:
    name = f"Custom {CompanyRole.unscoped.filter(company=company).count()}"
    role = CompanyRole.unscoped.create(company=company, name=name)
    if resource:
        RoleGrant.objects.create(role=role, resource=resource, **flags)
    return role


def custom_user(company: Company, role: CompanyRole, email: str = "custom@recipients.test") -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name="C",
            last_name="Custom",
            company_role=role,
        )


@pytest.mark.usefixtures("today")
def test_a_custom_role_with_receive_gets_contract_notices(firm):
    company = firm[0]
    role = custom_role(company, "notify_contract", can_receive=True)
    custom_user(company, role)
    contract_for(firm, end=TODAY + timedelta(days=30))

    assert contract_renewal_notice.call() == 3
    assert "custom@recipients.test" in notified(NotificationKind.CONTRACT_RENEWAL_DUE)


def test_holding_the_notice_means_the_receive_flag_on_that_resource(company):
    other_resource = custom_role(company, "notify_report", can_receive=True)
    read_only = custom_role(company, "notify_contract", can_read=True)
    custom_user(company, other_resource, "a@recipients.test")
    custom_user(company, read_only, "b@recipients.test")
    assert "a" not in roles_of(holders(company.pk, "notify_contract", RECEIVE))
    assert "b" not in roles_of(holders(company.pk, "notify_contract", RECEIVE))


def test_inactive_users_deleted_roles_and_other_firms_do_not_hear(company):
    role = custom_role(company, "notify_contract", can_receive=True)
    inactive = custom_user(company, role, "inactive@recipients.test")
    User.objects.filter(pk=inactive.pk).update(is_active=False)
    gone_role = custom_role(company, "notify_contract", can_receive=True)
    custom_user(company, gone_role, "gone@recipients.test")
    CompanyRole.unscoped.filter(pk=gone_role.pk).update(is_deleted=True)
    with system_context():
        elsewhere = Company.objects.create(legal_name="Else Ltd", display_name="Else")
    add_role_users_with_suffix(elsewhere)

    found = list(holders(company.pk, "notify_contract", RECEIVE))
    assert roles_of(found) == OWNER_ADMIN
    assert all(u.company_id == company.pk for u in found)


def add_role_users_with_suffix(company: Company) -> None:
    with system_context():
        for key in ("owner", "admin"):
            User.objects.create_user(
                email=f"{key}@elsewhere.test",
                password=PASSWORD,
                company=company,
                first_name=key,
                last_name="X",
                role=key,
            )


def test_a_user_is_listed_once_however_many_grants_the_role_has(company):
    role = custom_role(company, "notify_contract", can_receive=True)
    RoleGrant.objects.create(role=role, resource="notify_report", can_receive=True)
    custom_user(company, role)
    assert len(list(holders(company.pk, "notify_contract", RECEIVE))) == 3
