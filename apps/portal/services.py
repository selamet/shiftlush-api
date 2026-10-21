"""The portal account's life: invitation, sign-in, sessions, password reset.

Staff authentication (apps.users.services) is the model throughout, and its
primitives are reused rather than copied: the token generator and hash, the
lockout of spec 7.4 (in its own key space, see `_lockout_key`), the password
validators and hasher, and OneTimeToken. What is not shared is anything that
could let one kind of session stand in for the other — the session table, the
token audience, the cookie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.customers.models import Customer, CustomerContact
from apps.portal.models import PortalAccount, PortalRefreshSession
from apps.users import lockout
from apps.users.models import OneTimeToken, TokenPurpose
from apps.users.services import ACCESS_TOKEN_LIFETIME, REFRESH_TOKEN_LIFETIME, hash_token, new_token
from core import mail
from core.context import system_context
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError, DuplicateRecord

#: The audience portal access tokens carry. Staff tokens carry none; each
#: authentication class refuses the other's (core.authentication,
#: apps.portal.authentication).
PORTAL_AUDIENCE = "portal"
#: What every answer about an account names, joined wherever an account is read.
_ACCOUNT_JOINS = ("company", "customer", "contact")
INVITATION_LIFETIME = timedelta(hours=72)
PASSWORD_RESET_LIFETIME = timedelta(hours=1)


def _lockout_key(email: str) -> str:
    # The lockout is keyed on (e-mail, address). A building manager can also be
    # somebody's staff account under the same address; the prefix keeps their
    # portal failures from locking their staff sign-in, and the other way round.
    return f"portal:{email.lower()}"


@dataclass(frozen=True)
class TokenPair:
    access: str
    refresh: str
    refresh_expires_at: datetime


def issue_tokens(
    account: PortalAccount,
    *,
    user_agent: str = "",
    ip: str | None = None,
    continues: PortalRefreshSession | None = None,
) -> TokenPair:
    """An access token for the portal audience, and a refresh session.

    The access token names the account (`portal_account_id`) and nothing a
    staff view could key on: no `user_id`, no `company_id`, no role. Same
    lifetimes and the same signing key as staff tokens.
    """
    access = AccessToken()
    access.set_exp(lifetime=ACCESS_TOKEN_LIFETIME)
    access["aud"] = PORTAL_AUDIENCE
    access["portal_account_id"] = str(account.pk)

    refresh = new_token()
    expires_at = timezone.now() + REFRESH_TOKEN_LIFETIME
    PortalRefreshSession.objects.create(
        account=account,
        chain_id=continues.chain_id if continues else uuid4(),
        signed_in_at=continues.signed_in_at if continues else timezone.now(),
        token_hash=hash_token(refresh),
        expires_at=expires_at,
        user_agent=user_agent[:255],
        ip_address=ip,
    )
    return TokenPair(access=str(access), refresh=refresh, refresh_expires_at=expires_at)


def authenticate(*, email: str, password: str, ip: str | None) -> PortalAccount:
    """Verify credentials with the staff lockout rules (spec 7.4).

    Every failure is the same INVALID_CREDENTIALS, and an unknown address still
    pays for a hash, so the form cannot be used to find out who has an account.
    """
    email = email.lower()
    key = _lockout_key(email)
    if lockout.is_locked(email=key, ip=ip):
        raise BusinessRuleError(ErrorCode.ACCOUNT_LOCKED)

    with system_context():
        account = (
            # The account's firm, customer and contact come along: the response
            # names them, and outside a tenant context a lazy traversal would
            # find nothing through the tenant-scoped managers.
            PortalAccount.objects.select_related(*_ACCOUNT_JOINS)
            .filter(email=email, accepted_at__isnull=False)
            .order_by("-is_active", "-accepted_at", "id")
            .first()
        )

    if account is None or not account.check_password(password):
        if account is None:
            PortalAccount().set_password(password)
        lockout.record_failure(email=key, ip=ip)
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)
    if not account.is_active:
        raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)

    lockout.clear(email=key, ip=ip)
    account.last_login_at = timezone.now()
    with system_context():
        account.save(update_fields=["last_login_at", "updated_at"])
    return account


def rotate_refresh_token(
    *, refresh_token: str, user_agent: str = "", ip: str | None = None
) -> tuple[PortalAccount, TokenPair]:
    """Exchange a portal refresh token for a new pair; the staff rules exactly.

    Every refresh rotates. A revoked token coming back while its chain has a
    live successor is a replay, and every session of the account is ended; one
    from a chain that was deliberately ended is simply refused. See
    apps.users.services.rotate_refresh_token for the reasoning.
    """
    with system_context():
        session = (
            PortalRefreshSession.objects.select_related(
                *(f"account__{join}" for join in _ACCOUNT_JOINS)
            )
            .filter(token_hash=hash_token(refresh_token))
            .first()
        )
        if session is None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if session.revoked_at is not None:
            superseded = PortalRefreshSession.objects.filter(
                account_id=session.account_id, chain_id=session.chain_id, revoked_at__isnull=True
            ).exists()
            if superseded:
                with transaction.atomic():
                    PortalRefreshSession.objects.filter(
                        account_id=session.account_id, revoked_at__isnull=True
                    ).update(revoked_at=timezone.now())
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if session.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)
        account = session.account
        if not account.is_active or account.is_deleted:
            raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)
        with transaction.atomic():
            session.revoked_at = timezone.now()
            session.save(update_fields=["revoked_at"])
            pair = issue_tokens(account, user_agent=user_agent, ip=ip, continues=session)
    return account, pair


def revoke_refresh_token(refresh_token: str) -> None:
    PortalRefreshSession.objects.filter(
        token_hash=hash_token(refresh_token), revoked_at__isnull=True
    ).update(revoked_at=timezone.now())


def revoke_all_sessions(account: PortalAccount) -> None:
    PortalRefreshSession.objects.filter(account=account, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


# One-time tokens ----------------------------------------------------------------


def _issue_one_time_token(account: PortalAccount, purpose: str, lifetime: timedelta) -> str:
    # A new token retires any outstanding one of the same purpose, so a forwarded
    # older e-mail stops working the moment a fresh one is sent.
    OneTimeToken.objects.filter(
        portal_account=account, purpose=purpose, used_at__isnull=True
    ).update(used_at=timezone.now())
    token = new_token()
    OneTimeToken.objects.create(
        portal_account=account,
        purpose=purpose,
        token_hash=hash_token(token),
        expires_at=timezone.now() + lifetime,
    )
    return token


def _consume_one_time_token(token: str, purpose: str) -> PortalAccount:
    with system_context():
        record = (
            OneTimeToken.objects.select_related(
                *(f"portal_account__{join}" for join in _ACCOUNT_JOINS)
            )
            .filter(token_hash=hash_token(token), purpose=purpose)
            .first()
        )
        if record is None or record.used_at is not None or record.portal_account is None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if record.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
        return record.portal_account


# Invitation -------------------------------------------------------------------


@transaction.atomic
def invite(*, customer: Customer, contact: CustomerContact) -> tuple[PortalAccount, str]:
    """Invite a customer contact to the portal, or invite them again.

    The account exists from the invitation on, inactive and with no password,
    so the link can be single-use OneTimeToken like every other mailed link.
    Inviting again sends a fresh link and retires the old one; it also reopens a
    deactivated account once the contact accepts. An account that is already
    active is refused: it has a password, and a reset is the way back in.
    """
    if contact.customer_id != customer.pk:
        raise BusinessRuleError(ErrorCode.CONTACT_NOT_OF_CUSTOMER)
    email = contact.email.strip().lower()
    if not email:
        raise BusinessRuleError(ErrorCode.CONTACT_HAS_NO_EMAIL)

    account = PortalAccount.objects.filter(contact=contact).first()
    if account is not None and account.is_active:
        raise DuplicateRecord(ErrorCode.PORTAL_ACCOUNT_ALREADY_ACTIVE)
    with system_context():
        taken = PortalAccount.objects.filter(email=email, is_active=True).exists()
    if taken:
        raise DuplicateRecord(ErrorCode.PORTAL_EMAIL_IN_USE)

    if account is None:
        account = PortalAccount.objects.create(
            company_id=customer.company_id, customer=customer, contact=contact, email=email
        )
    else:
        account.email = email
        account.invited_at = timezone.now()
        account.save(update_fields=["email", "invited_at", "updated_at"])

    token = _issue_one_time_token(account, TokenPurpose.PORTAL_INVITATION, INVITATION_LIFETIME)
    company = customer.company
    mail.send_portal_invitation(
        to=email,
        full_name=contact.full_name,
        company_name=company.display_name,
        customer_name=customer.legal_name,
        token=token,
        valid_for_hours=int(INVITATION_LIFETIME.total_seconds() // 3600),
    )
    return account, token


@transaction.atomic
def accept(*, token: str, password: str) -> PortalAccount:
    """Set the password from the invitation link and open the account."""
    account = _consume_one_time_token(token, TokenPurpose.PORTAL_INVITATION)
    with system_context():
        if account.is_deleted or account.contact.is_deleted:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if (
            PortalAccount.objects.filter(email=account.email, is_active=True)
            .exclude(pk=account.pk)
            .exists()
        ):
            # Somebody else's account took the address while the link was in a
            # mailbox. Refused rather than allowed to collide.
            raise DuplicateRecord(ErrorCode.PORTAL_EMAIL_IN_USE)
        account.set_password(password)
        account.is_active = True
        account.accepted_at = timezone.now()
        account.deactivated_at = None
        account.save(
            update_fields=["password", "is_active", "accepted_at", "deactivated_at", "updated_at"]
        )
    return account


@transaction.atomic
def deactivate(account: PortalAccount) -> PortalAccount:
    """Close an account: no sign-in, and every session ends now."""
    account.is_active = False
    account.deactivated_at = timezone.now()
    account.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    revoke_all_sessions(account)
    return account


# Password reset ----------------------------------------------------------------


def request_password_reset(email: str) -> str | None:
    """Mail a reset link, or nothing — the caller answers the same either way."""
    with system_context():
        account = PortalAccount.objects.filter(email=email.lower(), is_active=True).first()
        if account is None:
            return None
        token = _issue_one_time_token(
            account, TokenPurpose.PORTAL_PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        contact_name = account.contact.full_name
    mail.send_portal_password_reset(to=account.email, full_name=contact_name, token=token)
    return token


@transaction.atomic
def confirm_password_reset(
    *, token: str, new_password: str, ip: str | None = None
) -> PortalAccount:
    account = _consume_one_time_token(token, TokenPurpose.PORTAL_PASSWORD_RESET)
    if not account.is_active:
        raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)
    account.set_password(new_password)
    with system_context():
        account.save(update_fields=["password", "updated_at"])
    lockout.clear(email=_lockout_key(account.email), ip=ip)
    revoke_all_sessions(account)
    return account


def close_accounts_of(contact: CustomerContact) -> int:
    """Deactivate every live account of a contact that has been deleted."""
    closed = 0
    for account in PortalAccount.unscoped.filter(contact=contact, is_deleted=False, is_active=True):
        with system_context():
            deactivate(account)
        closed += 1
    return closed
