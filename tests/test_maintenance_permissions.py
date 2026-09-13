"""The permission matrix entries a maintenance visit needs.

A technician may create and read a visit but not edit or delete one -
see the design doc's "Karar bekleyen bulgu" section and
resource_by_action, the same mechanism the QR-label endpoint already uses.
"""

from __future__ import annotations

from apps.users.models import Role
from core.permissions import READ, WRITE, may


class TestMaintenanceVisitPermissions:
    def test_technician_can_read_and_create(self):
        assert may(Role.TECHNICIAN, "maintenance_visit", READ)
        assert may(Role.TECHNICIAN, "maintenance_visit", WRITE)

    def test_technician_cannot_edit_or_delete(self):
        assert not may(Role.TECHNICIAN, "maintenance_visit_edit", WRITE)
        assert not may(Role.TECHNICIAN, "maintenance_visit_edit", READ)

    def test_owner_and_admin_can_edit(self):
        assert may(Role.OWNER, "maintenance_visit_edit", WRITE)
        assert may(Role.ADMIN, "maintenance_visit_edit", WRITE)

    def test_accountant_has_no_access(self):
        assert not may(Role.ACCOUNTANT, "maintenance_visit", READ)
        assert not may(Role.ACCOUNTANT, "maintenance_visit", WRITE)
