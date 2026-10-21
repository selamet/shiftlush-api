from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.db import models
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.attachments.models import Attachment
from apps.elevators.models import Elevator
from apps.inspections import rules
from apps.inspections.models import (
    Inspection,
    InspectionKind,
    Label,
    NonConformity,
    NonConformityStatus,
)
from core.error_codes import ErrorCode
from core.exceptions import field_error
from core.scheduling import local_today

# Three fields below are called `label`, the regulation's word. DRF's stubs
# declare `Field.label` (the human-readable name) on every serializer field, so
# mypy reads a declared field of that name as overriding it; at runtime the
# metaclass collects declared fields off the class and nothing is overridden.


class StrictMixin:
    """Rejects a field the client sent that this serializer does not declare.

    Copied rather than imported, as in every other app.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(getattr(self, "initial_data", {})) - set(self.fields)  # type: ignore[attr-defined]
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        validated: dict[str, Any] = super().validate(attrs)  # type: ignore[misc]
        return validated


class NonConformitySerializer(serializers.ModelSerializer[NonConformity]):
    class Meta:
        model = NonConformity
        fields = [
            "id",
            "inspection",
            "description",
            "severity",
            "status",
            "fixed_on",
            "fault",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class NonConformityEntrySerializer(serializers.Serializer[Any]):
    """One defect, as the inspection form sends it."""

    description = serializers.CharField(max_length=2000)
    severity = serializers.ChoiceField(choices=Label.choices)


class NonConformityEditSerializer(StrictMixin, serializers.ModelSerializer[NonConformity]):
    status = serializers.ChoiceField(choices=NonConformityStatus.choices, required=False)

    class Meta:
        model = NonConformity
        fields = ["status", "fixed_on", "description"]


class InspectionReadSerializer(serializers.ModelSerializer[Inspection]):
    elevator_name = serializers.CharField(source="elevator.name", read_only=True)
    building_name = serializers.CharField(source="elevator.building.name", read_only=True)
    customer = serializers.UUIDField(source="elevator.building.customer_id", read_only=True)
    customer_name = serializers.CharField(
        source="elevator.building.customer.legal_name", read_only=True
    )
    days_left = serializers.SerializerMethodField(
        help_text="Days until `correction_deadline` by the Istanbul calendar; negative once "
        "it has passed, null when the label needs no correction."
    )
    lift_usable = serializers.SerializerMethodField(
        help_text="Whether the regulation lets the lift run under this label."
    )
    sealed_if_missed = serializers.SerializerMethodField(
        help_text="Whether missing `correction_deadline` gets the lift sealed."
    )
    nonconformities = NonConformitySerializer(many=True, read_only=True)

    class Meta:
        model = Inspection
        fields = [
            "id",
            "elevator",
            "elevator_name",
            "building_name",
            "customer",
            "customer_name",
            "kind",
            "follows",
            "inspected_on",
            "inspection_body",
            "report_number",
            "label",
            "report",
            "next_inspection_due",
            "correction_deadline",
            "days_left",
            "lift_usable",
            "sealed_if_missed",
            "nonconformities",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_days_left(self, inspection: Inspection) -> int | None:
        if inspection.correction_deadline is None:
            return None
        return (inspection.correction_deadline - local_today()).days

    def get_lift_usable(self, inspection: Inspection) -> bool:
        return rules.RULES[Label(inspection.label)].usable

    def get_sealed_if_missed(self, inspection: Inspection) -> bool:
        return rules.RULES[Label(inspection.label)].sealed_if_missed


def _check_dates(attrs: dict[str, Any], instance: Inspection | None) -> None:
    inspected_on = attrs.get("inspected_on", getattr(instance, "inspected_on", None))
    next_due = attrs.get("next_inspection_due", getattr(instance, "next_inspection_due", None))
    if inspected_on and next_due and next_due <= inspected_on:
        raise field_error("next_inspection_due", ErrorCode.END_DATE_BEFORE_START_DATE)


class InspectionCreateSerializer(StrictMixin, serializers.ModelSerializer[Inspection]):
    """What the inspector's report says. The deadline is the server's."""

    elevator = serializers.PrimaryKeyRelatedField(queryset=Elevator.objects.none())
    follows = serializers.PrimaryKeyRelatedField(
        queryset=Inspection.objects.none(), required=False, allow_null=True
    )
    kind = serializers.ChoiceField(
        choices=InspectionKind.choices, required=False, default=InspectionKind.PERIODIC
    )
    label = serializers.ChoiceField(choices=Label.choices)  # type: ignore[assignment]
    next_inspection_due = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Defaults to a year after `inspected_on`; for a follow-up, to the date "
        "the inspection it follows set.",
    )
    nonconformities = NonConformityEntrySerializer(many=True, required=False)

    class Meta:
        model = Inspection
        fields = [
            "elevator",
            "kind",
            "follows",
            "inspected_on",
            "inspection_body",
            "report_number",
            "label",
            "next_inspection_due",
            "nonconformities",
        ]

    def get_fields(self) -> dict[str, Any]:
        # Built per request: the tenant manager fixes a queryset's company when
        # the queryset is created, and at import time there is none.
        fields = super().get_fields()
        cast(
            serializers.PrimaryKeyRelatedField[Elevator], fields["elevator"]
        ).queryset = Elevator.objects.all()
        cast(
            serializers.PrimaryKeyRelatedField[Inspection], fields["follows"]
        ).queryset = Inspection.objects.filter(kind=InspectionKind.PERIODIC)
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        follows: Inspection | None = attrs.get("follows")
        if attrs.get("kind", InspectionKind.PERIODIC) == InspectionKind.FOLLOW_UP:
            if follows is None:
                raise serializers.ValidationError(
                    {
                        "follows": serializers.ErrorDetail(
                            "A follow-up follows something.", "required"
                        )
                    }
                )
            if follows.elevator_id != attrs["elevator"].pk:
                raise serializers.ValidationError(
                    {"follows": serializers.ErrorDetail("Another lift's inspection.", "invalid")}
                )
            if attrs["inspected_on"] < follows.inspected_on:
                raise field_error("inspected_on", ErrorCode.END_DATE_BEFORE_START_DATE)
        elif follows is not None:
            raise serializers.ValidationError(
                {"follows": serializers.ErrorDetail("Only a follow-up follows.", "invalid")}
            )
        _check_dates(attrs, None)
        return attrs


class InspectionEditSerializer(StrictMixin, serializers.ModelSerializer[Inspection]):
    """Corrections to a recorded inspection, and its report PDF.

    The lift and the kind are what the record is; changing them would be a
    different inspection, so they are not here.
    """

    label = serializers.ChoiceField(choices=Label.choices, required=False)  # type: ignore[assignment]
    report = serializers.PrimaryKeyRelatedField(
        queryset=Attachment.objects.none(),
        required=False,
        allow_null=True,
        help_text="One of this inspection's own attachments, of category `inspection_report`.",
    )

    class Meta:
        model = Inspection
        fields = [
            "inspected_on",
            "inspection_body",
            "report_number",
            "label",
            "next_inspection_due",
            "report",
        ]

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        cast(
            serializers.PrimaryKeyRelatedField[Attachment], fields["report"]
        ).queryset = Attachment.objects.all()
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        _check_dates(attrs, self.instance)
        return attrs


class DueType(models.TextChoices):
    INSPECTION = "inspection", "Periodic inspection"
    CORRECTION = "correction", "Correction deadline"


class DueItemSerializer(serializers.Serializer[Any]):
    """One thing due: a periodic inspection, or a correction deadline."""

    type = serializers.ChoiceField(choices=DueType.choices)
    date = serializers.DateField()
    days_left = serializers.IntegerField()
    elevator = serializers.UUIDField()
    elevator_name = serializers.CharField()
    building_name = serializers.CharField()
    customer_name = serializers.CharField()
    label = serializers.CharField(help_text="The lift's current label.")  # type: ignore[assignment]
    inspection = serializers.UUIDField(allow_null=True)
    open_nonconformities = serializers.IntegerField()
    sealed_if_missed = serializers.BooleanField()


class OpenCorrectionMixin(serializers.Serializer[Any]):
    """The lift's open correction deadline, for every elevator read.

    A red or yellow label carries a legal deadline that ends in a seal, and the
    next periodic inspection a year away is false reassurance beside it (audit
    P1-06). Read from the annotations of `inspections.services.with_open_correction`
    on a list; worked out for the one lift otherwise.
    """

    correction_deadline = serializers.SerializerMethodField(
        help_text="The current inspection's correction deadline while a defect is still "
        "open; null when nothing is open."
    )
    correction_days_left = serializers.SerializerMethodField(
        help_text="Days until `correction_deadline` by the Istanbul calendar; negative "
        "once passed; null when nothing is open."
    )
    open_nonconformities = serializers.SerializerMethodField(
        help_text="Open defects of the current inspection (and of the periodic one a "
        "follow-up follows)."
    )

    def _open(self, elevator: Elevator) -> tuple[date | None, int]:
        from apps.inspections.services import open_correction

        # Kept on the instance: three fields ask, and a lift read without the
        # annotations (a create or edit response) should be worked out once.
        known: tuple[date | None, int] | None = getattr(elevator, "_open_correction", None)
        if known is None:
            known = open_correction(elevator)
            elevator._open_correction = known  # type: ignore[attr-defined]
        return known

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_correction_deadline(self, elevator: Elevator) -> str | None:
        # ISO, like every other date this API returns.
        deadline = self._open(elevator)[0]
        return deadline.isoformat() if deadline else None

    def get_correction_days_left(self, elevator: Elevator) -> int | None:
        deadline = self._open(elevator)[0]
        return None if deadline is None else (deadline - local_today()).days

    def get_open_nonconformities(self, elevator: Elevator) -> int:
        return self._open(elevator)[1]
