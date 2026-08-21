"""The permission matrix, checked exhaustively.

Every role against every resource and both actions, compared against the table
in the specification. Written out rather than generated from MATRIX itself —
asserting the code matches the code would prove nothing.
"""

from __future__ import annotations

import pytest

from apps.users.models import Role
from core.permissions import MATRIX, READ, WRITE, may

ALL_ROLES = [Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN, Role.ACCOUNTANT]

# resource, action -> roles that are allowed, transcribed from section 6.2.
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("company", READ): set(ALL_ROLES),
    ("company", WRITE): {Role.OWNER},
    ("user", READ): {Role.OWNER, Role.ADMIN},
    ("user", WRITE): {Role.OWNER, Role.ADMIN},
    ("customer", READ): set(ALL_ROLES),
    ("customer", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    ("building", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("building", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    ("elevator", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("elevator", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    ("contract", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.ACCOUNTANT},
    ("contract", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    ("contract_financials", READ): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    ("qr_label", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("audit_log", READ): {Role.OWNER, Role.ADMIN},
    ("audit_log", WRITE): set(),
}


@pytest.mark.parametrize(("key", "allowed"), EXPECTED.items())
def test_matrix_matches_the_specification(key, allowed):
    resource, action = key
    for role in ALL_ROLES:
        assert may(role, resource, action) is (role in allowed), (
            f"{role} on {resource}:{action} does not match the specification"
        )


class TestSpecificBoundaries:
    """The rules that exist for a reason worth restating."""

    def test_only_the_owner_writes_company_settings(self):
        assert may(Role.OWNER, "company", WRITE)
        assert not may(Role.ADMIN, "company", WRITE)

    def test_operations_cannot_see_contract_money(self):
        # Operations runs the fleet; the amount is not part of that job, and
        # the boundary is applied on the server as well as in the interface.
        assert may(Role.OPERATIONS, "contract", READ)
        assert not may(Role.OPERATIONS, "contract_financials", READ)

    def test_accountant_cannot_see_the_technical_register(self):
        assert may(Role.ACCOUNTANT, "contract", READ)
        assert not may(Role.ACCOUNTANT, "elevator", READ)
        assert not may(Role.ACCOUNTANT, "building", READ)

    def test_technician_reads_but_never_writes(self):
        for resource in ("customer", "building", "elevator"):
            assert may(Role.TECHNICIAN, resource, READ)
            assert not may(Role.TECHNICIAN, resource, WRITE)

    def test_technician_has_no_access_to_contracts(self):
        assert not may(Role.TECHNICIAN, "contract", READ)

    def test_audit_log_is_never_writable(self):
        # Append-only by design: a log anyone can edit is not evidence.
        for role in ALL_ROLES:
            assert not may(role, "audit_log", WRITE)


class TestFailClosed:
    def test_unknown_resource_is_denied(self):
        assert not may(Role.OWNER, "not_a_resource", READ)

    def test_unknown_role_is_denied(self):
        assert not may("superuser", "elevator", READ)

    def test_missing_role_is_denied(self):
        assert not may(None, "elevator", READ)

    def test_every_resource_declares_both_actions(self):
        # A resource missing an action would fall through to an empty set, which
        # denies — safe, but silently. Better to notice it here.
        for resource, actions in MATRIX.items():
            assert READ in actions, f"{resource} does not declare a read rule"
            assert WRITE in actions, f"{resource} does not declare a write rule"
