"""One narrow exception on top of the resource matrix.

A technician cannot write any attachment per core.permissions.MATRIX - see
the design doc's "Karar bekleyen bulgu" - but must be able to attach an
optional photo to their own maintenance visit. Every other combination of
object_type/category still falls through to the matrix and is denied.
Widening the matrix entry itself was rejected: it would also open signed
contracts and CE certificates to a technician, which is not the intent.
"""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.views import APIView

from apps.attachments.models import AttachmentCategory, ObjectType
from apps.attachments.services import peek_object_type_and_category
from apps.users.models import Role
from core.permissions import RolePermission
from core.requests import authenticated_company_id


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

        action = getattr(view, "action", None)
        if action == "upload_url":
            return (
                data.get("object_type") == ObjectType.MAINTENANCE_VISIT
                and data.get("category") == AttachmentCategory.PHOTO
            )
        if action == "create":
            storage_key = data.get("storage_key", "")
            object_type, category = peek_object_type_and_category(
                authenticated_company_id(request), storage_key
            )
            return (
                object_type == ObjectType.MAINTENANCE_VISIT and category == AttachmentCategory.PHOTO
            )
        return False
