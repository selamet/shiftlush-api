"""Custom user, roles, invitations, sessions and one-time tokens."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from core.fields import EncryptedCharField
from core.models import CompanyOwnedModel, SoftDeleteModel


class Role(models.TextChoices):
    # Stored in English; the Turkish labels live in the frontend translation
    # file, never in the database and never in this enum.
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    OPERATIONS = "operations", "Operations"
    TECHNICIAN = "technician", "Technician"
    ACCOUNTANT = "accountant", "Accountant"


class UserManager(BaseUserManager["User"]):
    """Deliberately not tenant-scoped.

    Authentication has to find a user before the company is known — the company
    comes *from* the user. A tenant-filtered default manager here would make
    login impossible in a way that looks like "wrong password".

    Tenant scoping for user *listing* is applied in the viewset's get_queryset
    instead, where the company is already in context.
    """

    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("Users must have an e-mail address.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            # An invited user has no password until they accept and choose one.
            # No administrator ever sets it on their behalf.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("A superuser must have is_staff and is_superuser set.")
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, SoftDeleteModel):
    """A person who signs in.

    Not a CompanyOwnedModel even though it carries a company: see UserManager
    for why the default manager must stay unscoped.
    """

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
    )

    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    # Globally unique, so one address belongs to one company. The consequence —
    # the same person cannot work at two firms — is accepted to keep sign-in
    # simple, and the API says so with EMAIL_ALREADY_REGISTERED rather than a
    # vague validation error.
    email = models.EmailField(max_length=150, unique=True)
    phone = models.CharField(max_length=20, blank=True)

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OPERATIONS)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_email_verified = models.BooleanField(default=False)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # No `failed_login_count` or `locked_until`. The specification's field list
    # (5.2) has both, and 7.4 then keys the lockout on the e-mail *and the
    # address* — which a column on this row cannot express, because the row is
    # the e-mail. Built the way the columns invite, the lock stops being a
    # defence and becomes a way for a stranger to hold a named customer out of
    # their own account. They are gone rather than left unread: an unused
    # `locked_until` is an invitation to write `if user.locked_until` somewhere
    # next year and put the hole straight back. The counter is in the cache,
    # keyed on the pair — see `apps.users.lockout` and the deviations table.

    # Ciphertext, not eleven digits: AES-256-GCM output does not fit a char(11),
    # and a column sized for plaintext is the clearest sign encryption was
    # bolted on afterwards.
    national_id = EncryptedCharField(max_length=255, blank=True)
    certificate_number = models.CharField(max_length=50, blank=True)
    certificate_valid_until = models.DateField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        db_table = "user"
        ordering = ["first_name", "last_name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=Role.values), name="user_role_valid")
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class UserCustomer(CompanyOwnedModel):
    """Which customers a technician is allowed to see.

    Only technicians are listed here; every other role sees the whole company.
    A technician with no rows sees an empty list, which is a stated condition
    rather than an error.
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="customer_assignments")
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="technician_assignments"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        db_table = "user_customer"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "customer"],
                condition=models.Q(is_deleted=False),
                name="uq_user_customer_active",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.customer_id}"


class Invitation(CompanyOwnedModel):
    email = models.EmailField(max_length=150)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    role = models.CharField(max_length=20, choices=Role.choices)
    # Only the hash is stored. The plaintext exists once, in the e-mail.
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="sent_invitations", null=True, blank=True
    )

    class Meta:
        db_table = "invitation"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="invitation_role_valid"
            )
        ]

    def __str__(self) -> str:
        return self.email


class RefreshSession(models.Model):
    """One row per issued refresh token.

    Not soft-deleted: `revoked_at` already carries that meaning, and an
    is_deleted column would give the same fact two sources of truth.

    A row is *not* a session in the sense a person means by the word. Rotation
    writes a new row every time the access token is refreshed, so one phone left
    signed in for a month is roughly three thousand rows. `chain_id` is what a
    person calls a session: it is minted at sign-in and carried forward by every
    rotation, so all the rows one device produced share it. `signed_in_at`
    travels the same way and records when the chain began, which `created_at`
    cannot — on every row after the first, `created_at` is the last rotation.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_sessions")
    # Not unique: sharing it is the point. It is what the API exposes as the
    # session id, so the id a client sees survives the rotation that happens
    # while it is looking at the screen.
    chain_id = models.UUIDField(default=uuid4, editable=False)
    signed_in_at = models.DateTimeField(default=timezone.now)
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "refresh_session"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
            # Revoking a session means revoking every row in its chain, and the
            # listing reads the live row of each chain. Both are user+chain.
            models.Index(fields=["user", "chain_id"]),
        ]

    def __str__(self) -> str:
        return f"session {self.pk} for {self.user_id}"


class TokenPurpose(models.TextChoices):
    PASSWORD_RESET = "password_reset", "Password reset"
    EMAIL_VERIFICATION = "email_verification", "E-mail verification"


class OneTimeToken(models.Model):
    """Password reset and e-mail verification.

    Single-use and short-lived, so `used_at` is the whole lifecycle; there is
    nothing for soft delete to preserve.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="one_time_tokens")
    purpose = models.CharField(max_length=32, choices=TokenPurpose.choices)
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "one_time_token"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "purpose", "used_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(purpose__in=TokenPurpose.values),
                name="one_time_token_purpose_valid",
            )
        ]

    def __str__(self) -> str:
        return f"{self.purpose} for {self.user_id}"
