"""Authentication and account flows.

Anything that touches more than one table, or needs a transaction, lives here
rather than in a view. Views serialise and authorise; they do not orchestrate.
Phase 2 calls the same functions from a mobile API, and a rule written in a view
would have to be rewritten there.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.companies.models import Company
from apps.users.models import (
    Invitation,
    OneTimeToken,
    RefreshSession,
    Role,
    TokenPurpose,
    User,
    UserCustomer,
)
from core import mail
from core.context import system_context
from core.error_codes import ErrorCode
from core.exceptions import BusinessRuleError

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
INVITATION_LIFETIME = timedelta(hours=72)
PASSWORD_RESET_LIFETIME = timedelta(hours=1)
EMAIL_VERIFICATION_LIFETIME = timedelta(hours=24)

MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


def _hash(token: str) -> str:
    """Tokens are stored hashed, never in the clear.

    A database dump should not hand over live sessions. SHA-256 rather than a
    password hash is right here: the token is already 256 bits of entropy, so
    there is nothing to brute force and no reason to pay for a slow hash on
    every refresh.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class TokenPair:
    access: str
    refresh: str
    refresh_expires_at: object


def issue_tokens(user: User, *, user_agent: str = "", ip: str | None = None) -> TokenPair:
    """Mint an access token and open a refresh session."""
    access = AccessToken.for_user(user)
    access.set_exp(lifetime=ACCESS_TOKEN_LIFETIME)
    # The company travels in the token so the tenant middleware does not have to
    # re-read the user on every request.
    access["company_id"] = str(user.company_id) if user.company_id else None
    access["role"] = user.role

    refresh = _new_token()
    expires_at = timezone.now() + REFRESH_TOKEN_LIFETIME
    RefreshSession.objects.create(
        user=user,
        token_hash=_hash(refresh),
        expires_at=expires_at,
        user_agent=user_agent[:255],
        ip_address=ip,
    )
    return TokenPair(access=str(access), refresh=refresh, refresh_expires_at=expires_at)


@transaction.atomic
def register_company(
    *,
    legal_name: str,
    display_name: str,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
) -> tuple[Company, User]:
    """Create a firm and its first owner together.

    Runs in system context because there is no company yet — the tenant guard
    would otherwise reject the first write a new customer ever makes. Both rows
    are created in one transaction: a company with no owner is unreachable, and
    an owner with no company cannot be scoped to anything.
    """
    if User.objects.filter(email=email.lower()).exists():
        raise BusinessRuleError(ErrorCode.EMAIL_ALREADY_REGISTERED)

    with system_context():
        company = Company.objects.create(legal_name=legal_name, display_name=display_name)
        owner = User.objects.create_user(
            email=email,
            password=password,
            company=company,
            first_name=first_name,
            last_name=last_name,
            role=Role.OWNER,
        )
    return company, owner


def authenticate(*, email: str, password: str) -> User:
    """Verify credentials, applying the lockout.

    Every failure path raises the same code. Telling the caller that an address
    is unknown turns the login form into a way to enumerate who has an account.
    """
    with system_context():
        user = User.objects.filter(email=email.lower()).first()

    if user is None:
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)

    now = timezone.now()
    if user.locked_until and user.locked_until > now:
        # Stated as its own code, because the interface says so up front rather
        # than springing it: a surprise lockout produces more support calls
        # than the lockout itself.
        raise BusinessRuleError(ErrorCode.ACCOUNT_LOCKED)

    if not user.check_password(password):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT_DURATION
            user.failed_login_count = 0
        user.save(update_fields=["failed_login_count", "locked_until", "updated_at"])
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)

    if not user.is_active:
        raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    user.save(update_fields=["failed_login_count", "locked_until", "last_login_at", "updated_at"])
    return user


def rotate_refresh_token(
    *, refresh_token: str, user_agent: str = "", ip: str | None = None
) -> TokenPair:
    """Exchange a refresh token for a new pair.

    Every refresh rotates: the presented token is revoked and a new one issued.

    If a token that has already been revoked comes back, it means a copy is in
    circulation — the legitimate holder rotated, and someone else is replaying
    the old one. There is no way to tell which party is which, so every session
    for that user is revoked and both are forced to sign in again. Logging the
    victim out is the cheap outcome; leaving the attacker with a live session is
    not.
    """
    token_hash = _hash(refresh_token)
    with system_context():
        session = RefreshSession.objects.filter(token_hash=token_hash).first()

        if session is None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)

        if session.revoked_at is not None:
            # The revocation gets its own transaction, which has to commit
            # before the error is raised. Wrapping the whole function in
            # atomic() would roll the revocation back on the way out — the
            # sessions would stay open and the defence would do nothing, while
            # every test that only checked the status code still passed.
            with transaction.atomic():
                RefreshSession.objects.filter(
                    user_id=session.user_id, revoked_at__isnull=True
                ).update(revoked_at=timezone.now())
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)

        if session.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)

        user = session.user
        if not user.is_active:
            raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)

        # Revoking the old session and issuing the new one must be one unit: a
        # crash between them would leave the caller with no working token.
        with transaction.atomic():
            session.revoked_at = timezone.now()
            session.save(update_fields=["revoked_at"])
            return issue_tokens(user, user_agent=user_agent, ip=ip)


def revoke_refresh_token(refresh_token: str) -> None:
    """Sign out of this session only. Other devices stay signed in."""
    with system_context():
        RefreshSession.objects.filter(
            token_hash=_hash(refresh_token), revoked_at__isnull=True
        ).update(revoked_at=timezone.now())


def revoke_all_sessions(user: User) -> None:
    with system_context():
        RefreshSession.objects.filter(user=user, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )


# --------------------------------------------------------------------------
# One-time tokens
# --------------------------------------------------------------------------


def _issue_one_time_token(user: User, purpose: TokenPurpose, lifetime: timedelta) -> str:
    with system_context():
        # Issuing a new token invalidates any outstanding one, so a forwarded
        # old e-mail cannot be used after a fresh request.
        OneTimeToken.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(
            used_at=timezone.now()
        )
        token = _new_token()
        OneTimeToken.objects.create(
            user=user,
            purpose=purpose,
            token_hash=_hash(token),
            expires_at=timezone.now() + lifetime,
        )
    return token


def _consume_one_time_token(token: str, purpose: TokenPurpose) -> User:
    with system_context():
        record = OneTimeToken.objects.filter(token_hash=_hash(token), purpose=purpose).first()
        if record is None or record.used_at is not None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if record.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
        return record.user


def request_password_reset(email: str) -> str | None:
    """Return a token, or None when the address is unknown.

    The caller sends the same response either way. Confirming which addresses
    exist would make this endpoint an account-enumeration tool.
    """
    with system_context():
        user = User.objects.filter(email=email.lower(), is_active=True).first()
    if user is None:
        return None

    token = _issue_one_time_token(user, TokenPurpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME)
    # Sent from here rather than from the view so the plaintext never leaves
    # this module. The return value exists for tests and for the console
    # backend; nothing in the request path reads it.
    mail.send_password_reset(to=user.email, first_name=user.first_name, token=token)
    return token


@transaction.atomic
def confirm_password_reset(*, token: str, new_password: str) -> User:
    user = _consume_one_time_token(token, TokenPurpose.PASSWORD_RESET)
    user.set_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    user.save(update_fields=["password", "failed_login_count", "locked_until", "updated_at"])
    # If the reset was triggered because the account was taken over, leaving the
    # attacker's sessions alive would defeat the point.
    revoke_all_sessions(user)
    return user


def request_email_verification(user: User) -> str:
    token = _issue_one_time_token(
        user, TokenPurpose.EMAIL_VERIFICATION, EMAIL_VERIFICATION_LIFETIME
    )
    mail.send_email_verification(to=user.email, first_name=user.first_name, token=token)
    return token


@transaction.atomic
def confirm_email_verification(token: str) -> User:
    user = _consume_one_time_token(token, TokenPurpose.EMAIL_VERIFICATION)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "updated_at"])
    return user


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


@transaction.atomic
def create_invitation(
    *, company: Company, email: str, first_name: str, last_name: str, role: str, invited_by: User
) -> tuple[Invitation, str]:
    """Invite a colleague. Returns the record and the plaintext token.

    The plaintext exists exactly once, in the e-mail. It is never stored, never
    shown to the administrator and never set by them: the invitee chooses their
    own password. Anything else is both a security hole and a data-protection
    problem.
    """
    if User.objects.filter(email=email.lower()).exists():
        raise BusinessRuleError(ErrorCode.EMAIL_ALREADY_REGISTERED)

    token = _new_token()
    invitation = Invitation.objects.create(
        company=company,
        email=email.lower(),
        first_name=first_name,
        last_name=last_name,
        role=role,
        token_hash=_hash(token),
        expires_at=timezone.now() + INVITATION_LIFETIME,
        invited_by=invited_by,
    )
    mail.send_invitation(
        to=invitation.email,
        first_name=invitation.first_name,
        company_name=company.display_name,
        token=token,
    )
    return invitation, token


@transaction.atomic
def resend_invitation(invitation: Invitation) -> str:
    """Issue a fresh token for an invitation and send it again.

    The old token stops working. Extending the deadline while leaving the
    previous link alive would mean two live credentials for one seat, and the
    usual reason for a resend is that the first mail went somewhere it should
    not have.
    """
    if invitation.accepted_at is not None:
        raise BusinessRuleError(ErrorCode.TOKEN_INVALID)

    token = _new_token()
    invitation.token_hash = _hash(token)
    invitation.expires_at = timezone.now() + INVITATION_LIFETIME
    invitation.save(update_fields=["token_hash", "expires_at", "updated_at"])

    mail.send_invitation(
        to=invitation.email,
        first_name=invitation.first_name,
        company_name=invitation.company.display_name,
        token=token,
    )
    return token


def invitation_for_token(token: str) -> Invitation:
    """Look up an invitation from the link, for the sign-up screen.

    Public: the invitee has no account yet. It reveals only the name and role
    already written in the e-mail they are holding, and only to someone who has
    the token.
    """
    with system_context():
        invitation = Invitation.objects.filter(token_hash=_hash(token)).first()
        if invitation is None or invitation.accepted_at is not None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if invitation.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)
        return invitation


@transaction.atomic
def accept_invitation(*, token: str, password: str) -> User:
    """Turn an invitation into an account.

    Runs in system context: the invitee has no session yet, so there is no
    company bound, and the tenant guard would reject the write.
    """
    with system_context():
        invitation = Invitation.objects.filter(token_hash=_hash(token)).first()
        if invitation is None or invitation.accepted_at is not None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)
        if invitation.expires_at <= timezone.now():
            raise BusinessRuleError(ErrorCode.TOKEN_EXPIRED)

        user = User.objects.create_user(
            email=invitation.email,
            password=password,
            company=invitation.company,
            first_name=invitation.first_name,
            last_name=invitation.last_name,
            role=invitation.role,
            is_email_verified=True,
        )
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at", "updated_at"])
    return user


def deactivate_user(*, user: User) -> None:
    """Leavers are deactivated, never deleted — their audit trail has to stay.

    A company must keep at least one active owner, or nobody can change company
    settings or manage users ever again.
    """
    if user.role == Role.OWNER:
        remaining = (
            User.objects.filter(company=user.company, role=Role.OWNER, is_active=True)
            .exclude(pk=user.pk)
            .count()
        )
        if remaining == 0:
            raise BusinessRuleError(ErrorCode.LAST_OWNER_CANNOT_BE_DEACTIVATED)

    user.is_active = False
    user.save(update_fields=["is_active", "updated_at"])
    revoke_all_sessions(user)


def _is_last_active_owner(user: User) -> bool:
    if user.role != Role.OWNER or not user.is_active:
        return False
    return (
        not User.objects.filter(company_id=user.company_id, role=Role.OWNER, is_active=True)
        .exclude(pk=user.pk)
        .exists()
    )


def change_role(*, user: User, role: str) -> None:
    """Move a user to another role.

    The same rule as deactivation, for the same reason: a company that loses its
    last owner has nobody who can manage users or company settings, and no way
    back in short of a database edit.
    """
    if role != user.role and _is_last_active_owner(user):
        raise BusinessRuleError(ErrorCode.LAST_OWNER_CANNOT_BE_DEACTIVATED)

    user.role = role
    user.save(update_fields=["role", "updated_at"])

    if role != Role.TECHNICIAN:
        # Assignments only mean something for technicians. Leaving them behind
        # would silently narrow the person's view again if they ever moved back.
        user.customer_assignments.all().delete()


@transaction.atomic
def set_assigned_customers(*, user: User, customer_ids: list, assigned_by: User) -> None:
    """Replace the set of customers a technician may see.

    A replace rather than an add: the caller sends the list it wants to be true,
    which is the only form that can remove an assignment without a second
    endpoint and a second race.
    """
    if user.role != Role.TECHNICIAN:
        raise BusinessRuleError(ErrorCode.ONLY_TECHNICIANS_ARE_ASSIGNED)

    wanted = set(customer_ids)
    current = {row.customer_id: row for row in user.customer_assignments.all()}

    for customer_id, row in current.items():
        if customer_id not in wanted:
            # Soft-deleted, so the history of who could see what survives. The
            # unique constraint is conditional on is_deleted, so re-assigning
            # the same customer later is not a conflict.
            row.delete()

    for customer_id in wanted - set(current):
        UserCustomer.objects.create(
            company_id=user.company_id,
            user=user,
            customer_id=customer_id,
            assigned_by=assigned_by,
        )
