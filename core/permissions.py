"""Role-based permissions.

Two layers that are deliberately separate and must not be conflated:

  - the tenant boundary decides *whose* records exist for you at all, and lives
    in the manager and in save() (see core.context);
  - permissions decide *what you may do* with the records that survived that.

Collapsing them into one check is how systems end up with a role that can see
across tenants because someone reused the wrong helper.

Object-level checks sit on top of the queryset filter, never instead of it. The
filter keeps a record out of a list; the object check keeps it out of a direct
fetch by id. Both are needed, because a client that guesses an id never touches
the list endpoint.
"""

from __future__ import annotations

from typing import Any, TypeVar

from django.db.models import Model, QuerySet
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.users.models import Role

# Generic so that a caller narrowing a Building queryset is handed Buildings
# back, not bare Models.
ModelT = TypeVar("ModelT", bound=Model)

# The matrix from the specification, written once. A viewset names a resource
# and the action it is performing; the answer comes from here rather than from
# a condition repeated across nine modules.
READ = "read"
WRITE = "write"

MATRIX: dict[str, dict[str, set[str]]] = {
    "company": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN, Role.ACCOUNTANT},
        # Only the owner changes company settings.
        WRITE: {Role.OWNER},
    },
    "user": {
        READ: {Role.OWNER, Role.ADMIN},
        WRITE: {Role.OWNER, Role.ADMIN},
    },
    # The one row of 6.2 the administrator is not ticked on, while every other
    # user-management row ticks them. Listing, inviting and editing a colleague are
    # an administrator's job; ending someone's access is the owner's. Written as
    # its own resource rather than by narrowing "user"/WRITE, because narrowing
    # that would take editing away from the administrator too. There is nothing
    # to read here, so nobody may read it.
    "user_deactivation": {
        READ: set(),
        WRITE: {Role.OWNER},
    },
    # Separate from "user" although the roles match today: inviting somebody is
    # not the same act as editing a colleague's record, and the day they need
    # to be granted separately this table is where it happens.
    "invitation": {
        READ: {Role.OWNER, Role.ADMIN},
        WRITE: {Role.OWNER, Role.ADMIN},
    },
    "customer": {
        # The technician is here but sees only assigned customers; that
        # narrowing happens in the queryset, not in this table.
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    "building": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    "complex": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    "elevator": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    "contract": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    # Not a resource of its own but a slice of the contract. Operations runs the
    # fleet, accounting runs the money, and neither needs the other's column.
    "contract_financials": {
        READ: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    },
    "qr_label": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    },
    "audit_log": {
        READ: {Role.OWNER, Role.ADMIN},
        WRITE: set(),
    },
    "attachment": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
}


def may(role: str | None, resource: str, action: str) -> bool:
    if role is None:
        return False
    return role in MATRIX.get(resource, {}).get(action, set())


class RolePermission(permissions.BasePermission):
    """Checks the matrix for the viewset's declared resource.

    Every viewset sets `resource`; a viewset that forgets is denied rather than
    allowed, so the failure mode of forgetting is a broken endpoint in testing
    rather than an open one in production.

    A viewset may also declare `resource_by_action` for the actions that are a
    different permission from the rest of the resource. Printing a QR label is
    the case that forced this: it hangs off the elevator endpoint but a
    technician may do it, while editing an elevator is not theirs to do.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False

        overrides = getattr(view, "resource_by_action", {})
        resource = overrides.get(getattr(view, "action", None)) or getattr(view, "resource", None)
        if resource is None:
            return False

        action = READ if request.method in permissions.SAFE_METHODS else WRITE
        return may(getattr(user, "role", None), resource, action)


class TechnicianScopedQueryset:
    """Narrows a queryset to the customers a technician is assigned to.

    A mixin rather than a permission, because it changes what exists rather than
    what is allowed — and because a technician with no assignments should see an
    empty list, not a 403. Nothing went wrong; they simply have no work yet.
    """

    #: Lookup path from this model to the customer, e.g. "building__customer".
    customer_path: str = "customer"

    def scope_to_assignments(self, queryset: QuerySet[ModelT], user: Any) -> QuerySet[ModelT]:
        if getattr(user, "role", None) != Role.TECHNICIAN:
            return queryset
        assigned = user.customer_assignments.values_list("customer_id", flat=True)
        return queryset.filter(**{f"{self.customer_path}__in": list(assigned)})
