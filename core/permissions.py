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
    "maintenance_visit": {
        # A technician records their own visit; Operations can too, on
        # behalf of a technician without an account yet. Accounting has no
        # reason to see this and Operations already covers Owner/Admin's
        # oversight need without adding them here separately.
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    },
    # A visit is not a technician's to correct after the fact - see
    # resource_by_action on MaintenanceVisitViewSet, the same mechanism the
    # QR-label endpoint uses to split "print" from "edit the elevator".
    "maintenance_visit_edit": {
        READ: set(),
        WRITE: {Role.OWNER, Role.ADMIN},
    },
    # The monthly plan (M3). A technician reads it — it is their work list,
    # narrowed in PlannedVisitViewSet to their own rows and the unassigned rows
    # of their customers — but deciding who goes where is the office's. One
    # resource rather than a read/edit pair like the visit's: every write here
    # (PATCH, bulk-assign) belongs to the same three roles. Accounting has no
    # reason to see it, as with visits.
    # Faults (M4). Anyone who goes to lifts may report one, and the technician
    # works the ones sent to them (arrive, resolve — narrowed to the assigned
    # technician in FaultViewSet). Sending, closing, cancelling and correcting
    # are the office's, as `fault_office`. Accounting reads closed faults
    # through billing (M7), not here.
    "fault": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
    },
    "fault_office": {
        READ: set(),
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    # Periodic inspections (M5). The office records what the inspection body
    # found and arranges the fixes; technicians read it for the lifts of their
    # customers (narrowed in the viewsets), because they do the fixing.
    # Accounting has no access.
    "inspection": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    # Running the customer portal (M6): inviting a customer's contacts, seeing
    # their accounts, closing one. The office's job; a technician and the
    # accountant have no say in who outside the firm can sign in.
    "portal_access": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    # Invoice drafts (M7) — the first module where the accountant is the main
    # user. Operations and technicians have no part in billing.
    "invoice": {
        READ: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    },
    # Monthly customer reports. The accountant reads them (they are what the
    # customer was told); only the office makes them, and regenerating replaces
    # a file that was already sent, so it is narrower.
    "customer_report": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    "customer_report_regenerate": {
        READ: set(),
        WRITE: {Role.OWNER, Role.ADMIN},
    },
    "planned_visit": {
        READ: {Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.TECHNICIAN},
        WRITE: {Role.OWNER, Role.ADMIN, Role.OPERATIONS},
    },
    # The ledger (F0). Reading accounts, cash accounts, statements and the
    # summary. Operations sees a customer's balance only, through the customer
    # read, not here; the technician has no part in money until F1. Nothing is
    # written through this resource: every write is a document, below.
    "ledger": {
        READ: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
        WRITE: set(),
    },
    # Collections and payments out. Operations takes the money at the door, so
    # it records them, but reads the ledger no further than above.
    "ledger_document": {
        READ: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT, Role.OPERATIONS},
    },
    # What a collection or a payment out is recorded against: the cash
    # accounts and suppliers to pick from. Operations records those documents,
    # so it reads these lists, and nothing else of the ledger: not movements,
    # statements, documents or the summary. Nothing is written through it.
    "ledger_picker": {
        READ: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT, Role.OPERATIONS},
        WRITE: set(),
    },
    # E-mailing a statement to the customer: a POST, but it moves no money, so
    # it is for whoever reads the ledger. Operations has no part in statements.
    "ledger_statement_send": {
        READ: set(),
        WRITE: {Role.OWNER, Role.ADMIN, Role.ACCOUNTANT},
    },
    # Manual entries, transfers, opening balances and the closed-through date:
    # the books themselves, the owner's and the accountant's alone.
    "ledger_admin": {
        READ: {Role.OWNER, Role.ACCOUNTANT},
        WRITE: {Role.OWNER, Role.ACCOUNTANT},
    },
}


def may(role: str | None, resource: str, action: str) -> bool:
    if role is None:
        return False
    return role in MATRIX.get(resource, {}).get(action, set())


def may_see_balances(role: str | None) -> bool:
    """Whether a customer's ledger balance is shown to this role.

    Whoever reads the ledger, and whoever records money against a customer
    (operations, "balances only, the customer page"); not the technician.
    """
    return may(role, "ledger", READ) or may(role, "ledger_document", WRITE)


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
