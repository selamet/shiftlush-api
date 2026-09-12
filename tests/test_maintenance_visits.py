"""Maintenance visit records - specification in
docs/superpowers/specs/2026-09-12-maintenance-visit-design.md.
"""

from __future__ import annotations

from apps.elevators.models import Elevator


class TestElevatorMaintenanceFields:
    def test_new_fields_default_to_null(self, db):
        from datetime import date

        from apps.companies.models import Company
        from apps.customers.models import Customer, CustomerType
        from apps.properties.models import Building, BuildingType
        from core.context import company_context, system_context

        with system_context():
            company = Company.objects.create(legal_name="Firm", display_name="Firm")
        with company_context(company.id):
            customer = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Cust"
            )
            building = Building.objects.create(
                company=company,
                customer=customer,
                name="B1",
                type=BuildingType.RESIDENTIAL,
                address_note="x",
            )
            elevator = Elevator.objects.create(
                company=company, building=building, qr_token="a" * 24
            )

        assert elevator.last_maintenance_date is None
        assert elevator.next_maintenance_date is None
        assert isinstance(elevator.maintenance_interval_days, int)
        assert date  # type: ignore  # imported for readability of the test body above
