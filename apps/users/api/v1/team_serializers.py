"""Serializers for managing colleagues, as opposed to signing in.

Kept apart from the authentication serializers because they answer a different
question. The auth ones describe *you*; these describe the people an owner or an
administrator manages, and they must never grow a password field — accounts are
created by invitation, and an administrator who can set a password can read one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rest_framework import serializers

from apps.users.models import Invitation, Role, User
from core.validators import normalize_email, normalize_phone


class UserSerializer(serializers.ModelSerializer[User]):
    full_name = serializers.CharField(read_only=True)
    assigned_customer_ids = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "role",
            "is_active",
            "is_email_verified",
            "certificate_number",
            "certificate_valid_until",
            "last_login_at",
            "assigned_customer_ids",
            "created_at",
        ]
        # `password`, `national_id`, `failed_login_count` and `locked_until` are
        # all on this model and none of them belong in a response. The field
        # list is explicit so that a new sensitive column is invisible here
        # until somebody adds it on purpose.
        read_only_fields = fields

    def get_assigned_customer_ids(self, user: User) -> list[str]:
        if user.role != Role.TECHNICIAN:
            return []
        return [str(row.customer_id) for row in user.customer_assignments.all()]


class UserUpdateSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        # Not the e-mail address: it is the username, it is globally unique, and
        # changing it silently would lock the person out of a password reset
        # they had already requested. That belongs in its own verified flow.
        fields = [
            "first_name",
            "last_name",
            "phone",
            "role",
            "certificate_number",
            "certificate_valid_until",
        ]

    def validate_phone(self, value: str) -> str:
        return normalize_phone(value) if value else value


class InvitationSerializer(serializers.ModelSerializer[Invitation]):
    invited_by_name = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        # `token_hash` is absent and must stay absent. It is a credential, and
        # its presence in a list response would put every pending invitation's
        # hash on a screen an administrator might screenshot.
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "expires_at",
            "accepted_at",
            "is_expired",
            "invited_by",
            "invited_by_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_invited_by_name(self, invitation: Invitation) -> str:
        return invitation.invited_by.full_name if invitation.invited_by else ""

    def get_is_expired(self, invitation: Invitation) -> bool:
        from django.utils import timezone

        return invitation.accepted_at is None and invitation.expires_at <= timezone.now()


class InvitationCreateSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField(max_length=150)
    first_name = serializers.CharField(max_length=60)
    last_name = serializers.CharField(max_length=60)
    role = serializers.ChoiceField(choices=Role.choices)

    def validate_email(self, value: str) -> str:
        return normalize_email(value)

    def validate_role(self, value: str) -> str:
        # An invitation that could mint an owner would let an administrator
        # promote themselves past the one role they do not hold.
        if value == Role.OWNER:
            raise serializers.ValidationError("Owners are not created by invitation.")
        return value


class InvitationPreviewSerializer(serializers.Serializer[Any]):
    """What the sign-up screen may show before anyone has authenticated.

    Only what is already in the e-mail the invitee is holding: who invited them
    and under what name. Anything more would make a leaked link a way to read
    the company's data without accepting.
    """

    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    role = serializers.CharField()
    company_name = serializers.CharField()
    expires_at = serializers.DateTimeField()


class AssignedCustomersSerializer(serializers.Serializer[Any]):
    customer_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=True)

    def validate_customer_ids(self, value: list[UUID]) -> list[UUID]:
        from apps.customers.models import Customer

        unique = list(dict.fromkeys(value))
        # Scoped by the tenant manager, so an id from another company simply is
        # not found — and is reported as unknown rather than as forbidden.
        found = set(Customer.objects.filter(pk__in=unique).values_list("pk", flat=True))
        missing = [str(one) for one in unique if one not in found]
        if missing:
            raise serializers.ValidationError(f"Unknown customers: {', '.join(missing)}")
        return unique


class AcceptInvitationResultSerializer(serializers.Serializer[Any]):
    user: Any = UserSerializer()
