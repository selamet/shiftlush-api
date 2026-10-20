from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.notifications.models import EMAIL_ALWAYS, Notification, NotificationKind
from core.error_codes import ErrorCode


class NotificationSerializer(serializers.ModelSerializer[Notification]):
    # Declared rather than inferred so the contract says "an object" instead of
    # "any JSON value": `data` is always a flat mapping of facts, never a list.
    # The name collides with Serializer.data only as far as mypy can see: DRF's
    # metaclass pops declared fields off the class, so the property survives.
    data = serializers.DictField(read_only=True)  # type: ignore[assignment]

    class Meta:
        model = Notification
        fields = ["id", "kind", "subject_type", "subject_id", "data", "read_at", "created_at"]
        read_only_fields = fields


class UnreadCountSerializer(serializers.Serializer[Any]):
    count = serializers.IntegerField(read_only=True)


class UpdatedCountSerializer(serializers.Serializer[Any]):
    """How many notifications a bulk action changed."""

    count = serializers.IntegerField(read_only=True)


class NotificationPreferenceListSerializer(serializers.ListSerializer[Any]):
    """The PUT body: a list of `{kind, email}`, each kind at most once.

    The same kind twice is refused rather than resolved by "last one wins":
    a client sending both answers has a bug, and picking one for it hides that.
    """

    def validate(self, attrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kinds = [entry["kind"] for entry in attrs]
        if len(kinds) != len(set(kinds)):
            raise serializers.ValidationError("Each kind may appear only once.", code="unique")
        return attrs


class NotificationPreferenceSerializer(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(choices=NotificationKind.choices)
    email = serializers.BooleanField()
    locked = serializers.BooleanField(
        read_only=True,
        help_text="Always e-mailed, whatever `email` is set to; turning it off is refused.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Refused rather than stored and ignored: a settings screen that shows a
        # box unticked while the e-mails keep coming would be lying.
        if attrs["kind"] in EMAIL_ALWAYS and not attrs["email"]:
            code = ErrorCode.NOTIFICATION_EMAIL_LOCKED
            raise serializers.ValidationError(
                {"email": serializers.ErrorDetail(code.label, code=code.value)}
            )
        return attrs

    class Meta:
        list_serializer_class = NotificationPreferenceListSerializer
