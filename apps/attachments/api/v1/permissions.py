"""One narrow exception on top of the resource matrix.

A technician cannot write any attachment per core.permissions.MATRIX. The
design doc's "Attachment permission carve-out" paragraph
(docs/superpowers/specs/2026-09-12-maintenance-visit-design.md) spells out
the exception: a technician may `POST` to the attachment endpoint only when
`object_type == ObjectType.MAINTENANCE_VISIT` and
`category == AttachmentCategory.PHOTO`, and only for a visit belonging to a
customer they are assigned to - the same
tenant/technician-assignment narrowing `TechnicianScopedQueryset` and
`AttachmentViewSet._visible_to_technician` already apply on the read side.
Every other combination of object_type/category/visit still falls through
to the matrix and is denied. Widening the matrix entry itself was rejected:
it would also open signed contracts and CE certificates to a technician,
which is not the intent.
"""

from __future__ import annotations

import uuid
from typing import Any

from rest_framework.request import Request
from rest_framework.views import APIView

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.attachments.services import peek_object_type_and_category
from apps.maintenance.models import MaintenanceVisit
from apps.users.models import Role
from core.permissions import RolePermission
from core.requests import authenticated_company_id


def _technician_is_assigned_to_visit(user: Any, company_id: uuid.UUID, object_id: Any) -> bool:
    """Does this technician's customer assignment cover this maintenance visit?

    Mirrors the read-side narrowing in
    `apps.attachments.api.v1.views.AttachmentViewSet._visible_to_technician`
    and `core.permissions.TechnicianScopedQueryset`: a technician's rights
    stop at the customers they are assigned to, company membership alone is
    not enough.

    Read through `unscoped` with an explicit `company_id` filter, the same
    reasoning `apps.attachments.services._target_exists` already uses: a
    permission check must not depend on `company_context` being active.

    Fails closed - a missing or malformed `object_id` (not a real UUID) is
    "no", never a crash, same instinct `peek_object_type_and_category`
    already applies to a malformed storage key.
    """
    try:
        visit_id = uuid.UUID(str(object_id))
    except (TypeError, ValueError):
        return False
    assigned_customers = user.customer_assignments.values_list("customer_id", flat=True)
    return MaintenanceVisit.unscoped.filter(
        pk=visit_id,
        company_id=company_id,
        is_deleted=False,
        elevator__building__customer_id__in=assigned_customers,
    ).exists()


class AttachmentPermission(RolePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        if super().has_permission(request, view):
            return True

        user = request.user
        if not user or not user.is_authenticated or getattr(user, "role", None) != Role.TECHNICIAN:
            return False
        if request.method != "POST":
            return False

        # A multipart list body has no keys to read; treating it as empty
        # falls through to the plain `False` below, same as a missing key.
        data = request.data if isinstance(request.data, dict) else {}
        company_id = authenticated_company_id(request)

        action = getattr(view, "action", None)
        if action == "upload_url":
            if not (
                data.get("object_type") == ObjectType.MAINTENANCE_VISIT
                and data.get("category") == AttachmentCategory.PHOTO
            ):
                return False
            return _technician_is_assigned_to_visit(user, company_id, data.get("object_id"))
        if action == "create":
            storage_key = data.get("storage_key", "")
            object_type, object_id, category = peek_object_type_and_category(
                company_id, storage_key
            )
            if not (
                object_type == ObjectType.MAINTENANCE_VISIT and category == AttachmentCategory.PHOTO
            ):
                return False
            return _technician_is_assigned_to_visit(user, company_id, object_id)
        return False
