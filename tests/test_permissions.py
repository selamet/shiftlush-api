"""The permission matrix, checked exhaustively.

Every seeded role against every resource and both actions, compared against
the table in the specification. Written out rather than generated from the
catalogue itself — asserting the code matches the code would prove nothing.
"""

from __future__ import annotations

import pytest

from apps.companies.models import Company
from apps.users.models import CompanyRole, Role, User
from core.context import system_context
from core.permissions import DELETE, READ, WRITE, may

ALL_ROLES = [Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN, Role.ACCOUNTANT]


@pytest.fixture
def staff(db) -> dict[str, User]:
    """One user of each seeded role, in one firm."""
    with system_context():
        company = Company.objects.create(legal_name="Firm Ltd", display_name="Firm")
        return {
            role: User.objects.create_user(
                email=f"{role}@example.com",
                password="x-Strong-pass-1",
                company=company,
                first_name="A",
                last_name="B",
                role=role,
            )
            for role in ALL_ROLES
        }


# resource, action -> roles that are allowed, transcribed from section 6.2.
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("company", READ): set(ALL_ROLES),
    ("company", WRITE): {Role.OWNER},
    ("user", READ): {Role.OWNER, Role.ADMIN},
    ("user", WRITE): {Role.OWNER, Role.ADMIN},
    # User deactivation: the owner alone. The one user-management row of 6.2
    # where the administrator is not ticked.
    ("user_deactivation", READ): set(),
    ("user_deactivation", WRITE): {Role.OWNER},
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
    # M3: the plan is the technician's to read and the office's to arrange.
    ("planned_visit", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("planned_visit", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    # M4: everyone who goes to lifts reports and works faults; the office sends,
    # closes, cancels and corrects them. Accounting has no access.
    ("fault", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("fault", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    # M5: the office records inspections; technicians read them.
    ("inspection", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    ("inspection", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    # M6: who outside the firm may sign in to the portal is the office's call.
    ("portal_access", READ): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    ("portal_access", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    # M7: billing is the accountant's, with the owner and the administrator.
    ("invoice", READ): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    ("invoice", WRITE): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    ("fault_office", READ): set(),
    ("fault_office", WRITE): {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    # F0: the ledger. Read by the office's money roles; collections and
    # payments out recorded by operations too; the books themselves (manual,
    # transfer, opening, closed-through) by the owner and the accountant.
    ("ledger", READ): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    ("ledger", WRITE): set(),
    ("ledger_document", READ): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    ("ledger_document", WRITE): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT, Role.OPERATIONS},
    ("ledger_admin", READ): {Role.OWNER, Role.ACCOUNTANT},
    ("ledger_admin", WRITE): {Role.OWNER, Role.ACCOUNTANT},
    # The cash accounts and suppliers a collection or payment out is recorded
    # against: operations picks from them, and reads nothing else of the ledger.
    ("ledger_picker", READ): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT, Role.OPERATIONS},
    ("ledger_picker", WRITE): set(),
    # Sending a statement is the ledger readers' (operations gets none).
    ("ledger_statement_send", READ): set(),
    ("ledger_statement_send", WRITE): {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
}


@pytest.mark.parametrize(("key", "allowed"), EXPECTED.items())
def test_matrix_matches_the_specification(staff, key, allowed):
    resource, action = key
    for role in ALL_ROLES:
        assert may(staff[role], resource, action) is (role in allowed), (
            f"{role} on {resource}:{action} does not match the specification"
        )


class TestSpecificBoundaries:
    """The rules that exist for a reason worth restating."""

    def test_only_the_owner_writes_company_settings(self, staff):
        assert may(staff[Role.OWNER], "company", WRITE)
        assert not may(staff[Role.ADMIN], "company", WRITE)

    def test_only_the_owner_deactivates_a_colleague(self, staff):
        # An administrator manages people — invites them, edits them, moves them
        # between roles. Ending someone's access is where 6.2 stops them, and it
        # is a separate resource so that saying so does not also take editing
        # away.
        assert may(staff[Role.ADMIN], "user", WRITE)
        assert not may(staff[Role.ADMIN], "user_deactivation", WRITE)
        assert may(staff[Role.OWNER], "user_deactivation", WRITE)
        # Deactivating is a DELETE since actions that void a record are.
        assert not may(staff[Role.ADMIN], "user_deactivation", DELETE)
        assert may(staff[Role.OWNER], "user_deactivation", DELETE)

    def test_operations_cannot_see_contract_money(self, staff):
        # Operations runs the fleet; the amount is not part of that job, and
        # the boundary is applied on the server as well as in the interface.
        assert may(staff[Role.OPERATIONS], "contract", READ)
        assert not may(staff[Role.OPERATIONS], "contract_financials", READ)

    def test_accountant_cannot_see_the_technical_register(self, staff):
        assert may(staff[Role.ACCOUNTANT], "contract", READ)
        assert not may(staff[Role.ACCOUNTANT], "elevator", READ)
        assert not may(staff[Role.ACCOUNTANT], "building", READ)

    def test_technician_reads_but_never_writes(self, staff):
        for resource in ("customer", "building", "elevator"):
            assert may(staff[Role.TECHNICIAN], resource, READ)
            assert not may(staff[Role.TECHNICIAN], resource, WRITE)

    def test_technician_has_no_access_to_contracts(self, staff):
        assert not may(staff[Role.TECHNICIAN], "contract", READ)

    def test_audit_log_is_never_writable(self, staff):
        # Append-only by design: a log anyone can edit is not evidence.
        for role in ALL_ROLES:
            assert not may(staff[role], "audit_log", WRITE)


class TestFailClosed:
    def test_unknown_resource_is_denied(self, staff):
        assert not may(staff[Role.OWNER], "not_a_resource", READ)

    def test_a_role_the_company_made_and_granted_nothing_is_denied(self, staff):
        user = staff[Role.ADMIN]
        user.company_role = CompanyRole.unscoped.create(company=user.company, name="Custom")
        user.save()
        assert not may(User.objects.get(pk=user.pk), "elevator", READ)

    def test_missing_user_is_denied(self):
        assert not may(None, "elevator", READ)
