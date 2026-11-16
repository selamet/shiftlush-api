"""A company's starting roles, seeded from the permission catalogue."""

from __future__ import annotations

from apps.companies.models import Company
from apps.users.models import CompanyRole, Role, RoleGrant
from core.permission_catalogue import CATALOGUE, DELETE, READ, RECEIVE, SEED_KEYS, WRITE

#: The one seeded role that narrows to assigned customers and works in the field.
FIELD_ROLE = Role.TECHNICIAN.value


def seed_company_roles(company: Company) -> dict[str, CompanyRole]:
    """Create the five starting roles and their grants; safe to call again.

    A role that already exists under its seed key is kept as it is, whatever it
    has been renamed to. Grants are only ever added, so a second call creates
    nothing; a grant the company has since edited is left alone. The owner role
    gets no grants: what the owner may do is computed, never stored.
    """
    roles: dict[str, CompanyRole] = {}
    for key in SEED_KEYS:
        roles[key], _ = CompanyRole.unscoped.get_or_create(
            company=company,
            system_key=key,
            is_deleted=False,
            defaults={
                "name": Role(key).label,
                "is_owner": key == Role.OWNER,
                "assigned_only": key == FIELD_ROLE,
                "field_staff": key == FIELD_ROLE,
            },
        )
    grants = [
        RoleGrant(
            role=roles[key],
            resource=resource.key,
            can_read=READ in held,
            can_write=WRITE in held,
            can_delete=DELETE in held,
            can_receive=RECEIVE in held,
        )
        for resource in CATALOGUE.values()
        for key, held in resource.seeds.items()
        if key != Role.OWNER
    ]
    # One statement however many roles there are; an existing (role, resource)
    # pair is skipped by the unique constraint rather than overwritten.
    RoleGrant.objects.bulk_create(grants, ignore_conflicts=True)
    return roles
