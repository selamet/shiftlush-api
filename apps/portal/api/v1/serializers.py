"""What a customer contact may see — and, by omission, what they may not.

Every serializer here names its fields one by one and is read-only. None of
them derives from a staff serializer: a field added to a staff serializer next
month must not appear in the portal by inheritance. What is deliberately left
out, per the spec: the office's work notes, the billable flag, contract money,
the lift's technical data and internal notes, staff names beyond the
technician's first name, and public reporters' contact details.
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework import serializers

from apps.attachments.models import Attachment
from apps.elevators.models import Elevator
from apps.faults.models import Fault
from apps.inspections.models import Inspection, NonConformity
from apps.maintenance.models import MaintenanceVisit, MaintenanceVisitItem
from apps.portal.models import PortalAccount, PortalAccountStatus
from apps.users.api.v1.serializers import PasswordField, StrictSerializer


class PortalFileSerializer(serializers.ModelSerializer[Attachment]):
    """A file the contact may download, through `/portal/files/{id}/download-url/`."""

    class Meta:
        model = Attachment
        fields = ["id", "original_filename", "mime_type", "category"]
        read_only_fields = fields


class PortalElevatorSerializer(serializers.ModelSerializer[Elevator]):
    building_name = serializers.CharField(source="building.name", read_only=True)

    class Meta:
        model = Elevator
        fields = [
            "id",
            "name",
            "building",
            "building_name",
            "status",
            "last_maintenance_date",
            "next_maintenance_date",
            "inspection_label",
            "next_inspection_date",
        ]
        read_only_fields = fields


class PortalVisitItemSerializer(serializers.ModelSerializer[MaintenanceVisitItem]):
    class Meta:
        model = MaintenanceVisitItem
        # The result, not the technician's note on it: notes are written for
        # the office.
        fields = ["item", "result"]
        read_only_fields = fields


class PortalVisitSerializer(serializers.ModelSerializer[MaintenanceVisit]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    technician_first_name = serializers.CharField(source="technician.first_name", read_only=True)
    items = PortalVisitItemSerializer(many=True, read_only=True)
    photos = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceVisit
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "visit_date",
            "status",
            "technician_first_name",
            "parts_replaced",
            "items",
            "photos",
        ]
        read_only_fields = fields

    def get_photos(self, visit: MaintenanceVisit) -> list[dict[str, Any]]:
        files = self.context.get("files", {}).get(visit.pk, [])
        return cast(list[dict[str, Any]], PortalFileSerializer(files, many=True).data)


class PortalNonConformitySerializer(serializers.ModelSerializer[NonConformity]):
    class Meta:
        model = NonConformity
        fields = ["id", "description", "severity", "status", "fixed_on"]
        read_only_fields = fields


class PortalInspectionSerializer(serializers.ModelSerializer[Inspection]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    nonconformities = PortalNonConformitySerializer(many=True, read_only=True)

    class Meta:
        model = Inspection
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "kind",
            "inspected_on",
            "inspection_body",
            "report_number",
            "label",
            "next_inspection_due",
            # The report PDF, downloaded through /portal/files/{id}/download-url/.
            "report",
            "nonconformities",
        ]
        read_only_fields = fields


class PortalFaultSerializer(serializers.ModelSerializer[Fault]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    building_name = serializers.CharField(source="elevator.building.name", read_only=True)
    technician_first_name = serializers.SerializerMethodField()
    reported_by_me = serializers.SerializerMethodField()

    class Meta:
        model = Fault
        fields = [
            "id",
            "reference",
            "elevator",
            "elevator_name",
            "building_name",
            "channel",
            "status",
            "priority",
            "description",
            "reported_at",
            "assigned_at",
            "on_site_at",
            "resolved_at",
            "closed_at",
            "cancelled_at",
            "technician_first_name",
            "resolution_notes",
            "reported_by_me",
        ]
        read_only_fields = fields

    def get_technician_first_name(self, fault: Fault) -> str | None:
        return fault.technician.first_name if fault.technician else None

    def get_reported_by_me(self, fault: Fault) -> bool:
        account = self.context.get("account")
        return account is not None and fault.reported_by_portal_account_id == account.pk


class PortalFaultReportSerializer(StrictSerializer):
    """The public report's fields, from an identified contact."""

    elevator = serializers.PrimaryKeyRelatedField(queryset=Elevator.objects.none())
    description = serializers.CharField(max_length=1000)
    someone_trapped = serializers.BooleanField(required=False, default=False)
    elevator_stopped = serializers.BooleanField(required=False, default=False)

    def get_fields(self) -> dict[str, Any]:
        # Built per request, under the firm context the portal view has bound,
        # and narrowed to the account's customer: another customer's lift is
        # "does not exist", the same as one never registered.
        fields = super().get_fields()
        account = self.context.get("account")
        elevator = cast(serializers.PrimaryKeyRelatedField[Elevator], fields["elevator"])
        if isinstance(account, PortalAccount):
            elevator.queryset = Elevator.objects.filter(building__customer_id=account.customer_id)
        return fields


class PortalMeSerializer(serializers.ModelSerializer[PortalAccount]):
    contact_name = serializers.CharField(source="contact.full_name", read_only=True)
    customer_name = serializers.CharField(source="customer.legal_name", read_only=True)
    company_name = serializers.CharField(source="company.display_name", read_only=True)
    company_phone = serializers.CharField(source="company.phone", read_only=True)

    class Meta:
        model = PortalAccount
        fields = ["id", "email", "contact_name", "customer_name", "company_name", "company_phone"]
        read_only_fields = fields


class PortalTokenResponseSerializer(serializers.Serializer[Any]):
    access = serializers.CharField()
    account = PortalMeSerializer()


class PortalLoginSerializer(StrictSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_email(self, value: str) -> str:
        return value.lower()


class PortalAcceptSerializer(StrictSerializer):
    token = serializers.CharField()
    password = PasswordField()


class PortalPasswordResetRequestSerializer(StrictSerializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return value.lower()


class PortalPasswordResetConfirmSerializer(StrictSerializer):
    token = serializers.CharField()
    password = PasswordField()


# The office's side -------------------------------------------------------------


class PortalInvitationSerializer(StrictSerializer):
    contact = serializers.UUIDField()


class PortalAccountSerializer(serializers.ModelSerializer[PortalAccount]):
    contact_name = serializers.CharField(source="contact.full_name", read_only=True)
    status = serializers.ChoiceField(choices=PortalAccountStatus.choices, read_only=True)

    class Meta:
        model = PortalAccount
        fields = [
            "id",
            "email",
            "customer",
            "contact",
            "contact_name",
            "status",
            "invited_at",
            "accepted_at",
            "deactivated_at",
            "last_login_at",
        ]
        read_only_fields = fields
