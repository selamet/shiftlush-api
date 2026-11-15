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


class CompanyRole(CompanyOwnedModel):
    """A role a company gives its people: a name, a few flags and its grants.

    Every company starts with the five roles the app always had, each marked
    with its `system_key`; a company may add its own, which have none. The
    owner role is `is_owner` and holds no grant rows — what the owner may do is
    computed, so no edit can lock a firm out of its own settings.
    """

    name = models.CharField(max_length=60)
    description = models.TextField(blank=True)
    # The seed key this role was created as ("" for a role the company added).
    system_key = models.CharField(max_length=20, blank=True)
    is_owner = models.BooleanField(default=False)
    # Sees only the customers assigned to the holder.
    assigned_only = models.BooleanField(default=False)
    # Works in the field: can be sent on visits and assigned to faults.
    field_staff = models.BooleanField(default=False)

    class Meta:
        db_table = "company_role"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                condition=models.Q(is_deleted=False),
                name="uq_company_role_name_active",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(is_owner=True),
                name="uq_company_role_one_owner",
            ),
            models.UniqueConstraint(
                fields=["company", "system_key"],
                condition=~models.Q(system_key="") & models.Q(is_deleted=False),
                name="uq_company_role_system_key_active",
            ),
            # Narrowing to assigned customers only makes sense for field staff.
            models.CheckConstraint(
                condition=models.Q(assigned_only=False) | models.Q(field_staff=True),
                name="company_role_assigned_only_is_field_staff",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class RoleGrant(models.Model):
    """What one role may do with one catalogue resource.

    No row means nothing granted. RECEIVE (who is notified) is `can_receive`.
    """

    id = models.BigAutoField(primary_key=True)
    role = models.ForeignKey(CompanyRole, on_delete=models.CASCADE, related_name="grants")
    resource = models.CharField(max_length=60)
    can_read = models.BooleanField(default=False)
    can_write = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_receive = models.BooleanField(default=False)

    class Meta:
        db_table = "role_grant"
        constraints = [
            models.UniqueConstraint(fields=["role", "resource"], name="uq_role_grant_resource")
        ]

    def __str__(self) -> str:
        return f"{self.role_id}: {self.resource}"


def seeded_role(company_id: Any, system_key: str) -> CompanyRole:
    """The company's live seeded role for a seed key (`Role` value)."""
    if company_id is None:
        raise ValueError("A role can only be set once the company is known.")
    return CompanyRole.unscoped.get(company_id=company_id, system_key=system_key, is_deleted=False)


class SeedRoleCompat:
    """`role` as the seed key of `company_role`. Removed in Part E.

    Reads give the seed key ("" for a company-made role or no role at all);
    assignment, including `Model(role=...)`, picks the company's seeded role
    with that key. It exists so code and tests that speak in seed keys keep
    working while they move to `company_role`.
    """

    company_id: Any
    company_role: CompanyRole | None

    def loaded_company_role(self) -> CompanyRole | None:
        """`company_role`, read without a company in context, and cached."""
        role_id = getattr(self, "company_role_id", None)
        if role_id is None:
            return None
        field = type(self)._meta.get_field("company_role")  # type: ignore[attr-defined]
        if not field.is_cached(self):
            # Not through the relation: that goes through the tenant-scoped
            # manager, and a user's role is read where no company is in context
            # yet — signing in, issuing a token. It is the row the user points
            # at, so there is no other firm's data to step into.
            field.set_cached_value(self, CompanyRole.unscoped.get(pk=role_id))
        role: CompanyRole = field.get_cached_value(self)
        return role

    @property
    def role(self) -> str:
        role = self.loaded_company_role()
        return "" if role is None else str(role.system_key)

    @role.setter
    def role(self, system_key: str) -> None:
        self.company_role = seeded_role(self.company_id, str(system_key))


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


class User(SeedRoleCompat, AbstractBaseUser, PermissionsMixin, SoftDeleteModel):
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

    # Null only for a user with no company (a superuser).
    company_role = models.ForeignKey(
        CompanyRole, on_delete=models.PROTECT, related_name="users", null=True, blank=True
    )

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

    # See core.models.CompanyOwnedModel: replacing an inherited manager is not
    # something the plugin models. UserManager is deliberately not a
    # SoftDeleteManager — authentication has to find a user before the company
    # is known — which is the second half of what is being silenced here.
    objects = UserManager()  # type: ignore[misc,assignment]

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        db_table = "user"
        ordering = ["first_name", "last_name", "id"]

    def __str__(self) -> str:
        return self.email

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.company_role_id is None and self.company_id is not None:
            # A colleague created without a role is operations, as the column's
            # default made them before roles became rows.
            self.company_role = seeded_role(self.company_id, Role.OPERATIONS)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "company_role"}
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_scoped(self) -> bool:
        """Sees only the customers assigned to them, and acts on their own work only."""
        role = self.loaded_company_role()
        return role is not None and role.assigned_only

    @property
    def is_owner(self) -> bool:
        """Holds the company's owner role."""
        role = self.loaded_company_role()
        return role is not None and role.is_owner

    @property
    def is_field_staff(self) -> bool:
        """Can be given customers, and sent on visits and to faults."""
        role = self.loaded_company_role()
        return role is not None and role.field_staff


class UserCustomer(CompanyOwnedModel):
    """Which customers a field worker is given.

    Only field staff are listed here. For a scoped role (`assigned_only`) the
    rows are everything the person sees; one with no rows sees an empty list,
    which is a stated condition rather than an error.
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


class Invitation(SeedRoleCompat, CompanyOwnedModel):
    email = models.EmailField(max_length=150)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    company_role = models.ForeignKey(
        CompanyRole, on_delete=models.PROTECT, related_name="invitations"
    )
    # Only the hash is stored. The plaintext exists once, in the e-mail.
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="sent_invitations", null=True, blank=True
    )

    class Meta:
        db_table = "invitation"
        ordering = ["-created_at", "-id"]

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
        ordering = ["-created_at", "-id"]
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
    # The customer portal's (M6). Their own purposes, so that no staff flow can
    # ever consume one: every lookup filters on the purpose first.
    PORTAL_INVITATION = "portal_invitation", "Portal invitation"
    PORTAL_PASSWORD_RESET = "portal_password_reset", "Portal password reset"


#: The purposes that belong to a portal account rather than a staff user.
PORTAL_PURPOSES = frozenset({TokenPurpose.PORTAL_INVITATION, TokenPurpose.PORTAL_PASSWORD_RESET})


class OneTimeToken(models.Model):
    """Password reset and e-mail verification, for staff and portal accounts.

    Single-use and short-lived, so `used_at` is the whole lifecycle; there is
    nothing for soft delete to preserve.

    A token belongs to exactly one principal: a staff user, or (M6) a customer
    portal account — never both, and the purpose says which.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="one_time_tokens", null=True, blank=True
    )
    portal_account = models.ForeignKey(
        "portal.PortalAccount",
        on_delete=models.CASCADE,
        related_name="one_time_tokens",
        null=True,
        blank=True,
    )
    purpose = models.CharField(max_length=32, choices=TokenPurpose.choices)
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "one_time_token"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "purpose", "used_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(purpose__in=TokenPurpose.values),
                name="one_time_token_purpose_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, portal_account__isnull=True)
                    & ~models.Q(purpose__in=PORTAL_PURPOSES)
                )
                | (
                    models.Q(user__isnull=True, portal_account__isnull=False)
                    & models.Q(purpose__in=PORTAL_PURPOSES)
                ),
                name="one_time_token_one_principal",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} for {self.user_id or self.portal_account_id}"
