"""Permissions read from the user's company role (A3).

The seeded roles must answer exactly as the fixed table the app shipped with,
and DELETE - now an action of its own - must follow `can_delete`, not
`can_write`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework.test import APIClient

from apps.companies.models import Company
from apps.customers.models import Customer, CustomerType
from apps.users.models import CompanyRole, RoleGrant, User
from apps.users.services import issue_tokens
from core.context import company_context, system_context
from core.permission_catalogue import CATALOGUE, DELETE, READ, SEED_KEYS, WRITE
from core.permissions import RolePermission, effective_permissions, may
from tests.legacy_matrix import LEGACY_MATRIX

PASSWORD = "x-Strong-pass-1"


def make_company(name: str = "Firm") -> Company:
    with system_context():
        return Company.objects.create(legal_name=f"{name} Ltd", display_name=name)


def make_user(company: Company | None, role: str | None = None, email: str = "u@x.test") -> User:
    extra: dict[str, Any] = {"role": role} if role else {}
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name="A",
            last_name="B",
            **extra,
        )


def make_custom_role(company: Company, **grants: dict[str, bool]) -> CompanyRole:
    role = CompanyRole.unscoped.create(company=company, name="Custom")
    RoleGrant.objects.bulk_create(
        RoleGrant(role=role, resource=resource, **flags) for resource, flags in grants.items()
    )
    return role


def fresh(user: User) -> User:
    """The user as a request would load it: nothing cached on the instance."""
    return User.objects.get(pk=user.pk)


@pytest.mark.parametrize("resource", sorted(LEGACY_MATRIX))
@pytest.mark.parametrize("key", SEED_KEYS)
@pytest.mark.parametrize("action", [READ, WRITE])
def test_seeded_roles_answer_like_the_legacy_matrix(db, resource, key, action):
    user = make_user(make_company(), role=key, email=f"{key}@x.test")
    assert may(user, resource, action) == (key in LEGACY_MATRIX[resource][action])


@pytest.mark.parametrize(
    "resource",
    sorted(r for r in LEGACY_MATRIX if DELETE in CATALOGUE[r].actions),
)
@pytest.mark.parametrize("key", SEED_KEYS)
def test_seeded_roles_delete_where_they_used_to_write(db, resource, key):
    # Before DELETE was its own action, removing a record was a WRITE.
    user = make_user(make_company(), role=key, email=f"{key}@x.test")
    assert may(user, resource, DELETE) == (key in LEGACY_MATRIX[resource][WRITE])


def test_no_user_and_no_role_may_nothing(db):
    assert may(None, "customer", READ) is False
    assert effective_permissions(None) == {}
    loner = make_user(None, email="loner@x.test")
    assert loner.company_role_id is None
    assert may(loner, "customer", READ) is False
    assert effective_permissions(loner) == {}


def test_the_owner_may_every_action_of_every_resource(db):
    owner = make_user(make_company(), role="owner", email="owner@x.test")
    assert effective_permissions(owner) == {r.key: r.actions for r in CATALOGUE.values()}


def test_unknown_resource_is_denied(db):
    owner = make_user(make_company(), role="owner", email="owner@x.test")
    assert may(owner, "not_a_resource", READ) is False


def test_a_role_with_no_grants_may_nothing(db):
    company = make_company()
    user = make_user(company, email="custom@x.test")
    user.company_role = make_custom_role(company)
    user.save()
    assert effective_permissions(fresh(user)) == {}


def test_permissions_are_read_without_a_company_in_context(db):
    # Signing in, issuing a token and background jobs have no tenant set.
    user = make_user(make_company(), role="accountant", email="acct@x.test")
    assert may(fresh(user), "ledger", READ) is True


def test_permissions_are_loaded_once_per_user(db, django_assert_num_queries):
    user = fresh(make_user(make_company(), role="operations", email="ops@x.test"))
    # The role row and its grants; the role is shared with `user.role`.
    with django_assert_num_queries(2):
        assert may(user, "customer", WRITE)
    with django_assert_num_queries(0):
        assert not may(user, "ledger", READ)
        assert may(user, "customer", DELETE)
        effective_permissions(user)


def test_the_grants_are_one_query_once_the_role_is_known(db, django_assert_num_queries):
    user = fresh(make_user(make_company(), role="operations", email="ops@x.test"))
    assert user.role == "operations"
    with django_assert_num_queries(1):
        effective_permissions(user)


def test_the_owner_costs_no_grant_query(db, django_assert_num_queries):
    user = fresh(make_user(make_company(), role="owner", email="owner@x.test"))
    assert user.role == "owner"
    with django_assert_num_queries(0):
        assert may(user, "ledger_admin", DELETE)


def test_changing_the_seeded_role_forgets_what_was_worked_out(db):
    user = make_user(make_company(), role="technician", email="t@x.test")
    assert not may(user, "customer", WRITE)
    user.role = "operations"
    assert may(user, "customer", WRITE)


def test_custom_role_answers_from_its_grants(db):
    company = make_company()
    user = make_user(company, email="custom@x.test")
    user.company_role = make_custom_role(
        company,
        customer={"can_read": True, "can_write": True},
        invoice={"can_read": True, "can_delete": True},
    )
    user.save()
    perms = effective_permissions(fresh(user))
    assert perms == {"customer": {READ, WRITE}, "invoice": {READ, DELETE}}


# Through HTTP --------------------------------------------------------------------


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


@pytest.mark.parametrize(("can_delete", "status"), [(False, 403), (True, 204)])
def test_delete_follows_can_delete_not_can_write(db, can_delete, status):
    company = make_company()
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Customer"
        )
    user = make_user(company, email="custom@x.test")
    user.company_role = make_custom_role(
        company, customer={"can_read": True, "can_write": True, "can_delete": can_delete}
    )
    user.save()
    response = api_for(user).delete(reverse("customer-detail", args=[customer.pk]))
    assert response.status_code == status


def test_write_without_delete_still_edits(db):
    company = make_company()
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name="Customer"
        )
    user = make_user(company, email="custom@x.test")
    user.company_role = make_custom_role(company, customer={"can_read": True, "can_write": True})
    user.save()
    response = api_for(user).patch(
        reverse("customer-detail", args=[customer.pk]), {"legal_name": "Renamed"}
    )
    assert response.status_code == 200


# Every guarded route names an action its resource has ---------------------------


def _routes(resolver: URLResolver | None = None) -> list[URLPattern]:
    found: list[URLPattern] = []
    for entry in (resolver or get_resolver()).url_patterns:
        if isinstance(entry, URLResolver):
            found.extend(_routes(entry))
        else:
            found.append(entry)
    return found


def _guarded_checks() -> set[tuple[str, str, str]]:
    """(view, resource, action) for every method a RolePermission route serves."""
    checks: set[tuple[str, str, str]] = set()
    for route in _routes():
        cls = getattr(route.callback, "cls", None)
        if cls is None or not any(
            issubclass(p, RolePermission) for p in getattr(cls, "permission_classes", [])
        ):
            continue
        actions: dict[str, str] = getattr(route.callback, "actions", None) or {
            m: m for m in cls.http_method_names if hasattr(cls, m)
        }
        for method, name in actions.items():
            if method not in cls.http_method_names or method in ("head", "options"):
                continue
            override = getattr(cls, "resource_by_action", {}).get(name)
            if isinstance(override, tuple):
                resource, action = override
            else:
                resource = override or cls.resource
                action = RolePermission.action_for(method.upper())
            checks.add((cls.__name__, resource, action))
    return checks


def test_every_guarded_route_names_an_action_its_resource_has():
    checks = _guarded_checks()
    assert checks
    missing = sorted(
        (view, resource, action)
        for view, resource, action in checks
        if action not in getattr(CATALOGUE.get(resource), "actions", ())
    )
    assert missing == []


@pytest.mark.parametrize(
    ("view", "resource"),
    [
        ("FaultViewSet", "fault_office"),
        ("UserViewSet", "user_deactivation"),
        ("MaintenanceVisitViewSet", "maintenance_visit_edit"),
        ("CollectionViewSet", "ledger_document"),
        ("PaymentOutViewSet", "ledger_document"),
        ("ManualEntryViewSet", "ledger_admin"),
        ("TransferViewSet", "ledger_admin"),
        ("OpeningBalanceViewSet", "ledger_admin"),
    ],
)
def test_removing_or_voiding_a_record_is_delete(view, resource):
    assert (view, resource, DELETE) in _guarded_checks()


def test_a_method_the_view_does_not_serve_is_405_not_403(db):
    # Faults are never deleted, so nobody holds DELETE on them; the answer is
    # that the method does not exist, not that the user lacks a grant for it.
    company = make_company()
    user = make_user(company, role="technician", email="t@x.test")
    response = api_for(user).delete(reverse("fault-detail", args=[uuid.uuid4()]))
    assert response.status_code == 405
