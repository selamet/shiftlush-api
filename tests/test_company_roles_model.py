"""Company roles and their grants, as rows (A2).

Every firm is created with the five roles the app always had, granted what the
catalogue seeds; a user points at one of them, and `user.role` keeps reading
as the seed key until the compatibility property is removed.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.companies.models import Company
from apps.users.models import CompanyRole, Invitation, Role, RoleGrant, User
from apps.users.roles import seed_company_roles
from core.context import company_context, system_context
from core.permission_catalogue import CATALOGUE, DELETE, READ, RECEIVE, SEED_KEYS, WRITE

pytestmark = pytest.mark.django_db


def make_company(name: str = "Firm") -> Company:
    with system_context():
        return Company.objects.create(legal_name=f"{name} Ltd", display_name=name)


def make_user(company: Company, role: str, email: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password="x-Strong-pass-1",
            company=company,
            first_name="A",
            last_name="B",
            role=role,
        )


def roles_of(company: Company) -> dict[str, CompanyRole]:
    return {r.system_key: r for r in CompanyRole.unscoped.filter(company=company)}


def test_a_new_company_gets_the_five_roles():
    company = make_company()
    assert set(roles_of(company)) == set(SEED_KEYS)


def test_the_seeded_roles_are_named_after_the_role_labels():
    names = {key: role.name for key, role in roles_of(make_company()).items()}
    assert names == {key: Role(key).label for key in SEED_KEYS}


def test_only_the_technician_is_scoped_and_field_staff():
    roles = roles_of(make_company())
    flags = {key: (r.assigned_only, r.field_staff) for key, r in roles.items()}
    assert flags == {key: (key == "technician",) * 2 for key in SEED_KEYS}


def test_only_the_owner_role_is_the_owner():
    roles = roles_of(make_company())
    assert {key for key, r in roles.items() if r.is_owner} == {"owner"}


def test_grants_follow_the_catalogue_seeds_and_the_owner_has_none():
    roles = roles_of(make_company())
    assert not roles["owner"].grants.exists()
    for key in SEED_KEYS:
        if key == "owner":
            continue
        held = {
            g.resource: {
                action
                for action, flag in (
                    (READ, g.can_read),
                    (WRITE, g.can_write),
                    (DELETE, g.can_delete),
                    (RECEIVE, g.can_receive),
                )
                if flag
            }
            for g in roles[key].grants.all()
        }
        expected = {r.key: set(r.seeds[key]) for r in CATALOGUE.values() if key in r.seeds}
        assert held == expected, key


def test_seeding_twice_creates_nothing_new():
    company = make_company()
    roles_before = CompanyRole.unscoped.count()
    grants_before = RoleGrant.objects.count()
    with system_context():
        seeded = seed_company_roles(company)
    assert set(seeded) == set(SEED_KEYS)
    assert CompanyRole.unscoped.count() == roles_before
    assert RoleGrant.objects.count() == grants_before


def test_create_user_with_a_role_string_points_at_the_seeded_role():
    company = make_company()
    user = make_user(company, role=Role.ACCOUNTANT, email="a@x.test")
    assert user.company_role == roles_of(company)["accountant"]
    assert user.role == "accountant"
    # Read back with no company in context, the way sign-in reads it.
    assert User.objects.get(pk=user.pk).role == "accountant"


def test_a_role_resolves_within_the_users_own_company():
    mine, theirs = make_company("Mine"), make_company("Theirs")
    user = make_user(mine, role=Role.ADMIN, email="a@x.test")
    assert user.company_role is not None
    assert user.company_role.company_id == mine.pk
    assert user.company_role != roles_of(theirs)["admin"]


def test_assigning_role_moves_the_user_to_that_seeded_role():
    company = make_company()
    user = make_user(company, role=Role.OPERATIONS, email="a@x.test")
    user.role = Role.TECHNICIAN
    with system_context():
        user.save()
    assert User.objects.get(pk=user.pk).company_role_id == roles_of(company)["technician"].pk


def test_a_user_created_without_a_role_is_operations_as_before():
    company = make_company()
    with system_context():
        user = User.objects.create_user(
            email="a@x.test",
            password="x-Strong-pass-1",
            company=company,
            first_name="A",
            last_name="B",
        )
    assert user.role == Role.OPERATIONS


def test_a_custom_role_reads_as_no_seed_key():
    company = make_company()
    with system_context():
        custom = CompanyRole.objects.create(company=company, name="Night shift")
    user = make_user(company, role=Role.OPERATIONS, email="a@x.test")
    user.company_role = custom
    assert user.role == ""


def test_an_invitation_takes_a_role_string_too():
    company = make_company()
    with system_context():
        invitation = Invitation.objects.create(
            company=company,
            email="i@x.test",
            first_name="I",
            last_name="N",
            # The compat property, which the type checker does not know of.
            role=Role.TECHNICIAN,  # type: ignore[misc]
            token_hash="h",
            expires_at="2030-01-01T00:00Z",
        )
    assert invitation.company_role == roles_of(company)["technician"]
    assert invitation.role == "technician"


def test_a_role_in_use_cannot_be_hard_deleted():
    company = make_company()
    user = make_user(company, role=Role.ACCOUNTANT, email="a@x.test")
    assert user.company_role is not None
    with pytest.raises(ProtectedError), system_context():
        user.company_role.hard_delete()


def test_assigned_only_requires_field_staff():
    company = make_company()
    with pytest.raises(IntegrityError), transaction.atomic(), system_context():
        CompanyRole.objects.create(company=company, name="Odd", assigned_only=True)


def test_a_firm_has_one_owner_role():
    company = make_company()
    with pytest.raises(IntegrityError), transaction.atomic(), system_context():
        CompanyRole.objects.create(company=company, name="Second owner", is_owner=True)


def test_role_names_are_unique_within_a_firm_but_not_across():
    a, b = make_company("A"), make_company("B")
    with system_context():
        CompanyRole.objects.create(company=a, name="Night shift")
        CompanyRole.objects.create(company=b, name="Night shift")
        with pytest.raises(IntegrityError), transaction.atomic():
            CompanyRole.objects.create(company=a, name="Night shift")


def test_a_grant_is_unique_per_role_and_resource():
    role = roles_of(make_company())["admin"]
    with pytest.raises(IntegrityError), transaction.atomic():
        RoleGrant.objects.create(role=role, resource="customer")


def test_a_firm_sees_only_its_own_roles():
    mine, theirs = make_company("Mine"), make_company("Theirs")
    with company_context(mine.pk):
        visible = set(CompanyRole.objects.values_list("company_id", flat=True))
    assert visible == {mine.pk}
    assert CompanyRole.unscoped.filter(company=theirs).count() == len(SEED_KEYS)


def test_the_invitation_list_reads_each_role_without_a_query_per_row():
    # The suite's N+1 guard closes the database while a list is serialised, so
    # a role fetched per row fails this request.
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.users.services import issue_tokens

    company = make_company()
    owner = make_user(company, role=Role.OWNER, email="o@x.test")
    with system_context():
        for n, key in enumerate((Role.TECHNICIAN, Role.ACCOUNTANT)):
            Invitation.objects.create(
                company=company,
                email=f"i{n}@x.test",
                first_name="I",
                last_name="N",
                company_role=roles_of(company)[key],
                token_hash=f"h{n}",
                expires_at="2030-01-01T00:00Z",
            )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(owner).access}")
    response = client.get(reverse("invitation-list"))
    assert response.status_code == 200
    assert sorted(row["role"] for row in response.data["results"]) == ["accountant", "technician"]
