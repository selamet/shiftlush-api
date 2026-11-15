"""The data migration that gives every existing firm its roles (users 0008).

Tested against a real schema at users 0007: the test database is migrated back,
rows are written with the old `role` column through the historical models, and
the migration's own functions run forward and back. The schema is migrated
forward again however the test ends, so nothing after it sees the old columns.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from core.permission_catalogue import SEED_KEYS

BEFORE = ("users", "0007_company_roles")
SEED = importlib.import_module("apps.users.migrations.0008_seed_company_roles")

# Migrating the schema needs real commits, not the per-test transaction.
pytestmark = pytest.mark.django_db(transaction=True)


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


@pytest.fixture
def old_schema() -> Iterator[Any]:
    """The historical apps at users 0007, with the database migrated to match."""
    _migrate([BEFORE])
    try:
        yield MigrationExecutor(connection).loader.project_state(BEFORE).apps
    finally:
        # Emptied first: a test that failed half-way can leave rows the later
        # migrations cannot carry (an invitation with no role), and a test
        # database stuck at 0007 would fail every test after this one. The
        # transactional teardown empties the tables anyway.
        call_command("flush", interactive=False, verbosity=0)
        executor = MigrationExecutor(connection)
        _migrate(executor.loader.graph.leaf_nodes())


def _firm(apps: Any, name: str, *, deleted: bool = False) -> Any:
    return apps.get_model("companies", "Company").objects.create(
        legal_name=f"{name} Ltd", display_name=name, is_deleted=deleted
    )


def _user(apps: Any, company: Any, role: str, email: str, **extra: Any) -> Any:
    return apps.get_model("users", "User").objects.create(
        company=company, role=role, email=email, first_name="A", last_name="B", **extra
    )


def _invitation(apps: Any, company: Any, role: str, email: str) -> Any:
    return apps.get_model("users", "Invitation").objects.create(
        company=company,
        role=role,
        email=email,
        first_name="I",
        last_name="N",
        token_hash=f"hash-{email}",
        expires_at=timezone.now() + timedelta(days=3),
    )


def test_every_user_and_invitation_lands_on_its_own_firms_role(old_schema):
    apps = old_schema
    role_model = apps.get_model("users", "CompanyRole")
    grant_model = apps.get_model("users", "RoleGrant")
    user_model = apps.get_model("users", "User")
    invitation_model = apps.get_model("users", "Invitation")

    live, gone = _firm(apps, "Live"), _firm(apps, "Gone", deleted=True)
    people = {
        (live.pk, "owner"): _user(apps, live, "owner", "o@live.test"),
        (live.pk, "technician"): _user(apps, live, "technician", "t@live.test"),
        (gone.pk, "accountant"): _user(apps, gone, "accountant", "a@gone.test"),
    }
    invited = {
        (live.pk, "admin"): _invitation(apps, live, "admin", "i@live.test"),
        (gone.pk, "operations"): _invitation(apps, gone, "operations", "i@gone.test"),
    }
    superuser = _user(apps, None, "operations", "su@x.test", is_superuser=True, is_staff=True)

    SEED.seed_roles(apps, None)

    # (c) Both firms are seeded, the soft-deleted one included.
    for firm in (live, gone):
        keys = set(role_model.objects.filter(company=firm).values_list("system_key", flat=True))
        assert keys == set(SEED_KEYS)

    # (a) Each row points at its own firm's role for its old key.
    for model, rows in ((user_model, people), (invitation_model, invited)):
        for (company_id, key), row in rows.items():
            role = model.objects.get(pk=row.pk).company_role
            assert (role.company_id, role.system_key) == (company_id, key)

    # (b) No firm, so no role.
    assert user_model.objects.get(pk=superuser.pk).company_role_id is None

    # (d) A second run creates nothing.
    counts = (role_model.objects.count(), grant_model.objects.count())
    SEED.seed_roles(apps, None)
    assert (role_model.objects.count(), grant_model.objects.count()) == counts


def test_going_back_restores_the_keys_and_refuses_over_a_custom_role(old_schema):
    apps = old_schema
    user_model = apps.get_model("users", "User")
    firm = _firm(apps, "Firm")
    owner = _user(apps, firm, "owner", "o@firm.test")
    tech = _user(apps, firm, "technician", "t@firm.test")
    SEED.seed_roles(apps, None)

    # The column is blanked first, so only the reverse can put the key back.
    user_model.objects.filter(company=firm).update(role="")
    SEED.unseed_roles(apps, None)
    assert user_model.objects.get(pk=owner.pk).role == "owner"
    assert user_model.objects.get(pk=tech.pk).role == "technician"

    # (e) A role the firm made itself has no column value, so the reverse refuses.
    custom = apps.get_model("users", "CompanyRole").objects.create(company=firm, name="Night")
    user_model.objects.filter(pk=tech.pk).update(company_role=custom)
    with pytest.raises(RuntimeError, match="made itself"):
        SEED.unseed_roles(apps, None)
