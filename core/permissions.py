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

from apps.users.models import Role, RoleGrant
from core.permission_catalogue import CATALOGUE, RECEIVE

# Re-exported: views and tests name the actions next to `may()`, from here.
from core.permission_catalogue import DELETE as DELETE
from core.permission_catalogue import READ as READ
from core.permission_catalogue import WRITE as WRITE

# Generic so that a caller narrowing a Building queryset is handed Buildings
# back, not bare Models.
ModelT = TypeVar("ModelT", bound=Model)


def effective_permissions(user: Any) -> dict[str, frozenset[str]]:
    """What the user's company role may do: resource -> the actions it holds.

    The owner role holds every action of every catalogue resource, computed
    rather than stored, so no edit to the grants can lock a firm out of its own
    settings. Anyone else holds what their role's grant rows say; no role, or
    no company, holds nothing.

    A role that has been deleted, or that belongs to another firm, holds
    nothing: neither should happen, and if it does it must not open anything.

    Read without a company in context - signing in, issuing a token and
    background jobs have none - and remembered on the user instance, so a
    request pays for it once however many checks it makes. The memo is keyed
    by the role it was worked out for, so pointing the user at another role
    is never answered from the old one.
    """
    if user is None:
        return {}
    role_id = getattr(user, "company_role_id", None)
    cached: tuple[Any, dict[str, frozenset[str]]] | None = getattr(user, "_perm_cache", None)
    if cached is not None and cached[0] == role_id:
        return cached[1]
    perms = _load_permissions(user)
    user._perm_cache = (role_id, perms)
    return perms


def _load_permissions(user: Any) -> dict[str, frozenset[str]]:
    if getattr(user, "company_id", None) is None:
        return {}
    role = user.loaded_company_role() if hasattr(user, "loaded_company_role") else None
    if role is None or role.is_deleted or role.company_id != user.company_id:
        return {}
    if role.is_owner:
        return {r.key: r.actions for r in CATALOGUE.values()}
    # Not tenant-scoped: a grant belongs to the role, and the role is the one
    # the user points at.
    perms: dict[str, frozenset[str]] = {}
    for grant in RoleGrant.objects.filter(role_id=role.pk):
        held = frozenset(
            action
            for action, granted in (
                (READ, grant.can_read),
                (WRITE, grant.can_write),
                (DELETE, grant.can_delete),
                (RECEIVE, grant.can_receive),
            )
            if granted
        )
        if held:
            perms[grant.resource] = held
    return perms


def may(user: Any, resource: str, action: str) -> bool:
    """Whether the user's role holds `action` on `resource`. Unknown is no."""
    return action in effective_permissions(user).get(resource, frozenset())


def may_see_balances(user: Any) -> bool:
    """Whether a customer's ledger balance is shown to this user.

    Whoever reads the ledger, and whoever records money against a customer
    (operations, "balances only, the customer page"); not the technician.
    """
    return may(user, "ledger", READ) or may(user, "ledger_document", WRITE)


class RolePermission(permissions.BasePermission):
    """Checks the user's role for the viewset's declared resource.

    Every viewset sets `resource`; a viewset that forgets is denied rather than
    allowed, so the failure mode of forgetting is a broken endpoint in testing
    rather than an open one in production.

    A viewset may also declare `resource_by_action` for the actions that are a
    different permission from the rest of the resource. Printing a QR label is
    the case that forced this: it hangs off the elevator endpoint but a
    technician may do it, while editing an elevator is not theirs to do.

    The action follows the method: a safe method reads, `DELETE` deletes, and
    anything else writes. An entry in `resource_by_action` may name the action
    too, as a `(resource, action)` pair, for a custom action that removes or
    voids a record - a cancel or a deactivation is a POST, but granting it is
    granting DELETE, not WRITE.
    """

    @staticmethod
    def action_for(method: str) -> str:
        if method in permissions.SAFE_METHODS:
            return READ
        if method == "DELETE":
            return DELETE
        return WRITE

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False

        method = (request.method or "").lower()
        if method not in view.http_method_names or not hasattr(view, method):
            # Not served here at all: the view answers 405 for everyone. Left
            # to us, a method no resource has an action for (DELETE where
            # nothing is deleted) would read as a 403, as if it could be granted.
            return True

        overrides = getattr(view, "resource_by_action", {})
        override = overrides.get(getattr(view, "action", None))
        if isinstance(override, tuple):
            resource, action = override
        else:
            resource = override or getattr(view, "resource", None)
            action = self.action_for(request.method or "")
        if resource is None:
            return False
        return may(user, resource, action)


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
