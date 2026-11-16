"""The permission matrix entries a maintenance visit needs.

A technician may create and read a visit but not edit or delete one -
see the design doc's "Karar bekleyen bulgu" section and
resource_by_action, the same mechanism the QR-label endpoint already uses.
"""

from __future__ import annotations

import pytest

from apps.companies.models import Company
from apps.users.models import Role, User
from core.context import system_context
from core.permissions import DELETE, READ, WRITE, may


@pytest.fixture
def staff(db) -> dict[str, User]:
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
            for role in Role
        }


class TestMaintenanceVisitPermissions:
    def test_technician_can_read_and_create(self, staff):
        assert may(staff[Role.TECHNICIAN], "maintenance_visit", READ)
        assert may(staff[Role.TECHNICIAN], "maintenance_visit", WRITE)

    def test_technician_cannot_edit_or_delete(self, staff):
        assert not may(staff[Role.TECHNICIAN], "maintenance_visit_edit", WRITE)
        assert not may(staff[Role.TECHNICIAN], "maintenance_visit_edit", READ)
        assert not may(staff[Role.TECHNICIAN], "maintenance_visit_edit", DELETE)

    def test_owner_and_admin_can_edit(self, staff):
        assert may(staff[Role.OWNER], "maintenance_visit_edit", WRITE)
        assert may(staff[Role.ADMIN], "maintenance_visit_edit", WRITE)
        assert may(staff[Role.ADMIN], "maintenance_visit_edit", DELETE)

    def test_accountant_has_no_access(self, staff):
        assert not may(staff[Role.ACCOUNTANT], "maintenance_visit", READ)
        assert not may(staff[Role.ACCOUNTANT], "maintenance_visit", WRITE)
