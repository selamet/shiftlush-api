import pytest

from core.permission_catalogue import (
    CATALOGUE,
    DELETE,
    READ,
    RECEIVE,
    SEED_KEYS,
    WRITE,
)
from tests.legacy_matrix import LEGACY_MATRIX


def test_every_legacy_resource_is_in_the_catalogue():
    assert set(LEGACY_MATRIX) <= set(CATALOGUE)


@pytest.mark.parametrize("resource", sorted(LEGACY_MATRIX))
@pytest.mark.parametrize("key", SEED_KEYS)
def test_seeds_reproduce_the_legacy_matrix(resource, key):
    legacy = LEGACY_MATRIX[resource]
    seeds = CATALOGUE[resource].seeds.get(key, frozenset())
    assert (READ in seeds) == (key in legacy[READ])
    assert (WRITE in seeds) == (key in legacy[WRITE])
    if DELETE in CATALOGUE[resource].actions:
        assert (DELETE in seeds) == (key in legacy[WRITE])


def test_seeds_only_use_actions_the_resource_has():
    for r in CATALOGUE.values():
        for held in r.seeds.values():
            assert held <= r.actions


def test_seed_keys_are_the_known_roles():
    for r in CATALOGUE.values():
        assert set(r.seeds) <= set(SEED_KEYS)


def test_role_resource_is_seeded_on_owner_and_admin():
    assert CATALOGUE["role"].seeds["admin"] == {READ, WRITE, DELETE}
    assert "operations" not in CATALOGUE["role"].seeds


def test_notification_resources_match_todays_recipients():
    expected = {
        "notify_contract": {"owner", "admin"},  # contracts/tasks.py:37
        "notify_fault_office": {"owner", "admin", "operations"},  # faults/services.py:42
        "notify_inspection_office": {"owner", "admin", "operations"},  # inspections/services.py:42
        "notify_report": {"owner", "admin"},  # reports/services.py:38
        "notify_billing": {"owner", "admin", "accountant"},  # billing/services.py:45
        "notify_plan": {"owner", "admin", "operations"},  # maintenance/tasks.py:57
    }
    for key, holders in expected.items():
        assert CATALOGUE[key].actions == {RECEIVE}
        assert {k for k, a in CATALOGUE[key].seeds.items() if RECEIVE in a} == holders


def test_scopable_is_exactly_the_narrowed_resources():
    assert {k for k, r in CATALOGUE.items() if r.scopable} == {
        "customer",
        "building",
        "elevator",
        "maintenance_visit",
        "planned_visit",
        "fault",
        "inspection",
        "attachment",
        "qr_label",
    }


@pytest.mark.parametrize(
    "resource", ["maintenance_visit_edit", "fault_office", "user_deactivation"]
)
def test_resources_guarding_a_destroy_can_be_deleted(resource):
    # These are routed to by resource_by_action for destroy, cancel and
    # deactivate; without DELETE nobody could ever perform those actions.
    r = CATALOGUE[resource]
    assert r.actions == {WRITE, DELETE}
    assert {k for k, a in r.seeds.items() if DELETE in a} == {
        k for k, a in r.seeds.items() if WRITE in a
    }


def test_cancelling_the_books_is_delete_for_who_writes_them():
    # Manual entries, transfers and opening balances are voided by a cancel,
    # which checks DELETE; it stays with the owner and the accountant.
    r = CATALOGUE["ledger_admin"]
    assert DELETE in r.actions
    assert {k for k, a in r.seeds.items() if DELETE in a} == {"owner", "accountant"}


def test_closing_a_portal_account_is_delete_for_the_office():
    # The portal `deactivate` action checks DELETE; it stays with the office.
    r = CATALOGUE["portal_access"]
    assert DELETE in r.actions
    assert {k for k, a in r.seeds.items() if DELETE in a} == {"owner", "admin", "operations"}
