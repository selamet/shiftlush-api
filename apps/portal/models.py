"""The customer portal's principal, and its sessions.

M6: docs/superpowers/specs/2026-10-21-m6-customer-portal-design.md.

A `PortalAccount` is not a `User` with a sixth role. The staff API is guarded by
a role matrix written for five staff roles and by the technician narrowing; a
customer role would have to be denied in every endpoint anyone ever writes, and
the one that forgets leaks one customer's lifts to another. A separate principal
is denied by construction: staff views authenticate users only, portal views
authenticate portal accounts only, and neither accepts the other's token.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

from core.models import CompanyOwnedModel


class PortalAccountStatus(models.TextChoices):
    """What the office sees of an account. Derived, not stored."""

    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class PortalAccount(CompanyOwnedModel):
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="portal_accounts"
    )
    contact = models.ForeignKey(
        "customers.CustomerContact", on_delete=models.PROTECT, related_name="portal_accounts"
    )
    #: Lowercased on the way in, like User.email. Login has no firm to go by, so
    #: it is unique across every firm among the accounts that can sign in.
    email = models.EmailField(max_length=150)
    #: Hashed with the project's password hasher, like a staff password. Empty
    #: until the invitation is accepted, and `check_password` never matches an
    #: empty one.
    password = models.CharField(max_length=255, blank=True)
    #: Off until the invitation is accepted; off again when the firm closes the
    #: account or the contact is deleted.
    is_active = models.BooleanField(default=False)
    invited_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "portal_account"
        ordering = ["email", "id"]
        constraints = [
            # One account per contact.
            models.UniqueConstraint(
                fields=["contact"],
                condition=models.Q(is_deleted=False),
                name="uq_portal_account_per_contact",
            ),
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(is_active=True, is_deleted=False),
                name="uq_portal_account_active_email",
            ),
        ]
        indexes = [models.Index(fields=["email"])]

    def __str__(self) -> str:
        return self.email

    #: Read by DRF and the throttle, which treat whatever authentication returns
    #: as `request.user`. Always true: an account that is not signed in is never
    #: handed to a view.
    is_authenticated = True
    is_anonymous = False
    #: How core.idempotency tells this principal from a staff user.
    is_portal_account = True

    def set_password(self, raw: str) -> None:
        self.password = make_password(raw)

    def check_password(self, raw: str) -> bool:
        if not self.password:
            return False
        return bool(check_password(raw, self.password))

    @property
    def status(self) -> str:
        """What the office sees: invited, active or inactive."""
        if self.is_active:
            return PortalAccountStatus.ACTIVE
        if self.accepted_at is None and self.deactivated_at is None:
            return PortalAccountStatus.INVITED
        return PortalAccountStatus.INACTIVE


class PortalRefreshSession(models.Model):
    """One row per issued portal refresh token.

    The portal's copy of users.RefreshSession, and deliberately a copy rather
    than a shared table with a nullable user: the two sessions must never be
    exchangeable for each other, and a table that holds both is one wrong filter
    away from it. Same fields, same rotation and replay rules
    (apps.portal.services).
    """

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(
        PortalAccount, on_delete=models.CASCADE, related_name="refresh_sessions"
    )
    chain_id = models.UUIDField(default=uuid4, editable=False)
    signed_in_at = models.DateTimeField(default=timezone.now)
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "portal_refresh_session"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["account", "revoked_at"]),
            models.Index(fields=["account", "chain_id"]),
        ]

    def __str__(self) -> str:
        return f"portal session {self.pk} for {self.account_id}"
