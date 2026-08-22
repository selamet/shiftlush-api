"""Serializers for the authentication endpoints.

Read and write are separate classes throughout. A single serializer juggling
`read_only` is how a field like `company` ends up quietly writable, and how a
new sensitive column added to the model appears in the API by accident — which
is a breaking change nobody noticed making.

Fields are listed explicitly. `fields = "__all__"` would mean the API surface
changes whenever the model does.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.users.models import Role, User
from core.validators import normalize_email


class StrictSerializer(serializers.Serializer):
    """Rejects fields it does not know about.

    DRF ignores unknown keys by default, which silently swallows a typo: the
    client sends `emial`, gets a 200, and the value never arrives. Failing is
    the kinder answer.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "unexpected field"))
        return attrs


class PasswordField(serializers.CharField):
    """A password, checked against the project's validators.

    The length comes from the settings rather than a number written here. It
    used to be written in both places, which meant lowering the floor in the
    settings changed the error message and nothing else — the serializer went
    on refusing at the old length.

    No composition rules: requiring a symbol mostly produces Parola1!.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("write_only", True)
        kwargs.setdefault("style", {"input_type": "password"})
        kwargs.setdefault("min_length", settings.MIN_PASSWORD_LENGTH)
        super().__init__(**kwargs)

    def run_validation(self, data: Any = serializers.empty) -> Any:
        value = super().run_validation(data)
        # The owner, when the serializer can name one. Without it the
        # similarity validator is configured but inert — it has nothing to
        # compare against, so it silently permits a password that is just the
        # person's own surname.
        owner = getattr(self.parent, "password_owner", None)
        validate_password(value, user=owner() if owner else None)
        return value


class RegisterSerializer(StrictSerializer):
    legal_name = serializers.CharField(max_length=200)
    display_name = serializers.CharField(max_length=80)
    first_name = serializers.CharField(max_length=60)
    last_name = serializers.CharField(max_length=60)
    email = serializers.EmailField(max_length=150)
    password = PasswordField()

    def password_owner(self) -> User:
        """The person registering, for the similarity check.

        Unsaved, and built from the raw payload because the password is
        validated during field processing, before `validated_data` exists. Only
        the attributes the validator reads are set.
        """
        raw = self.initial_data if isinstance(self.initial_data, dict) else {}
        return User(
            email=str(raw.get("email") or ""),
            first_name=str(raw.get("first_name") or ""),
            last_name=str(raw.get("last_name") or ""),
        )

    def validate_email(self, value: str) -> str:
        return normalize_email(value)


class LoginSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=150)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_email(self, value: str) -> str:
        return normalize_email(value)


class PasswordResetRequestSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=150)

    def validate_email(self, value: str) -> str:
        return normalize_email(value)


class PasswordResetConfirmSerializer(StrictSerializer):
    token = serializers.CharField(max_length=128)
    password = PasswordField()


class EmailVerifySerializer(StrictSerializer):
    token = serializers.CharField(max_length=128)


class InvitationAcceptSerializer(StrictSerializer):
    token = serializers.CharField(max_length=128)
    password = PasswordField()


class CurrentUserSerializer(serializers.ModelSerializer):
    """What `/auth/me` returns.

    Read-only, and narrower than the model on purpose: `national_id`,
    `password`, `failed_login_count` and `locked_until` are all on User and none
    of them belong in a response.
    """

    company_id = serializers.UUIDField(read_only=True, allow_null=True)
    # Part of the session rather than a separate resource: the shell shows the
    # firm's name on every screen, and making that a second request puts an
    # independently failing call on the boot path for one string.
    company_name = serializers.SerializerMethodField()
    full_name = serializers.CharField(read_only=True)
    role = serializers.ChoiceField(choices=Role.choices, read_only=True)

    def get_company_name(self, user: User) -> str:
        # Empty string rather than null for a superuser with no company: the
        # client renders it directly, and widening the type for a case that
        # never reaches a real screen costs every call site a null check.
        return user.company.display_name if user.company_id else ""

    class Meta:
        model = User
        fields = [
            "id",
            "company_id",
            "company_name",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "role",
            "is_active",
            "is_email_verified",
            "last_login_at",
            "certificate_number",
            "certificate_valid_until",
        ]
        read_only_fields = fields


class TokenResponseSerializer(serializers.Serializer):
    """The body of a successful sign-in.

    Only the access token is here. The refresh token goes out as an httpOnly
    cookie and never touches JavaScript — putting it in the body would make it
    readable by any script that gets injected into the page.
    """

    access = serializers.CharField(read_only=True)
    user = CurrentUserSerializer(read_only=True)
