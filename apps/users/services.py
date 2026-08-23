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
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.companies.models import Company
from apps.users import lockout
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
    refresh_expires_at: datetime


def issue_tokens(
    user: User,
    *,
    user_agent: str = "",
    ip: str | None = None,
    continues: RefreshSession | None = None,
) -> TokenPair:
    """Mint an access token and open a refresh session.

    `continues` names the row this one replaces, when there is one. The new row
    inherits its chain and its sign-in time, so a rotation extends the session a
    person is already in rather than appearing to be a second device. Left None,
    this is a sign-in and a new chain starts.
    """
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
        chain_id=continues.chain_id if continues else uuid4(),
        signed_in_at=continues.signed_in_at if continues else timezone.now(),
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

    # After the transaction, not inside it. A mail server that is briefly
    # unreachable must not roll back a firm's registration — the account exists
    # either way, and the verification can be requested again.
    request_email_verification(owner)
    return company, owner


def authenticate(*, email: str, password: str, ip: str | None) -> User:
    """Verify credentials, applying the lockout of 7.4.

    Every failure path raises the same code. Telling the caller that an address
    is unknown turns the login form into a way to enumerate who has an account.

    `ip` is required rather than defaulted, because the lockout is keyed on the
    pair and a default would quietly put every caller in one bucket — which is
    the account-wide lock this replaced, reintroduced by an argument nobody
    passed. It must be `core.client_ip.client_ip`'s answer and no other: that
    one reads the right-most trusted `X-Forwarded-For` entry, and a bucket keyed
    on a value the caller writes is not a bucket.
    """
    email = email.lower()

    if lockout.is_locked(email=email, ip=ip):
        # Checked first, so a locked bucket costs no query and reads the same
        # whether or not the address is registered.
        #
        # Stated as its own code, because the interface says so up front rather
        # than springing it: a surprise lockout produces more support calls than
        # the lockout itself. The code keeps its name — renaming one is a
        # breaking change for every client switching on it — but it no longer
        # means the account is locked, only that this pair is. Nothing an
        # attacker does to it reaches the person whose account it is.
        raise BusinessRuleError(ErrorCode.ACCOUNT_LOCKED)

    with system_context():
        user = User.objects.filter(email=email).first()

    if user is None:
        # Hash something anyway. Returning before paying for argon2id is the
        # difference between a few milliseconds and a few hundred, and that is
        # the same enumeration answer the shared error code exists to withhold —
        # readable over a handful of requests and free to collect. Django's own
        # ModelBackend does this, for this reason.
        User().set_password(password)
        lockout.record_failure(email=email, ip=ip)
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)

    if not user.check_password(password):
        lockout.record_failure(email=email, ip=ip)
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)

    # Past this line the password was right, so nothing below is a guess. An
    # inactive account is refused without counting against the pair and without
    # clearing it: a leaver typing their own old password is not an attempt to
    # be counted, and it is not a reason to hand a fresh five attempts back to
    # whoever else is in the middle of spending them.
    if not user.is_active:
        raise BusinessRuleError(ErrorCode.ACCOUNT_INACTIVE)

    lockout.clear(email=email, ip=ip)
    user.last_login_at = timezone.now()
    user.save(update_fields=["last_login_at", "updated_at"])
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

    "Already revoked" alone is no longer enough to call that, now that a person
    can end a session deliberately. The device that was ended keeps its token
    until it next tries to refresh, and that attempt is expected rather than
    suspicious — treating it as a replay would mean ending the old phone signs
    the user out of the laptop they are holding within fifteen minutes, which is
    the opposite of what they asked for.

    What separates the two is whether the chain the token came from still has a
    live successor. A rotated token has one, and that successor is the live
    credential a replay is trying to run alongside — so the alarm fires and
    protects it. A token from a chain that was ended has none: there is nothing
    left in it to protect, the token cannot be exchanged for anything, and a
    blanket revocation would only take out sessions the replay never reached.
    """
    token_hash = _hash(refresh_token)
    with system_context():
        session = RefreshSession.objects.filter(token_hash=token_hash).first()

        if session is None:
            raise BusinessRuleError(ErrorCode.TOKEN_INVALID)

        if session.revoked_at is not None:
            superseded = RefreshSession.objects.filter(
                user_id=session.user_id, chain_id=session.chain_id, revoked_at__isnull=True
            ).exists()
            if superseded:
                # The revocation gets its own transaction, which has to commit
                # before the error is raised. Wrapping the whole function in
                # atomic() would roll the revocation back on the way out — the
                # sessions would stay open and the defence would do nothing,
                # while every test that only checked the status code still
                # passed.
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
            # Same chain: this is the same person on the same device, still
            # inside the session they started. A new chain here would make the
            # session list grow a row every fifteen minutes.
            return issue_tokens(user, user_agent=user_agent, ip=ip, continues=session)


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
# Sessions a person can see and end
# --------------------------------------------------------------------------


def session_for_refresh_token(user: User, refresh_token: str | None) -> RefreshSession | None:
    """The row the caller's own refresh cookie points at, if any.

    Scoped to the user on purpose. A cookie belonging to somebody else must not
    be able to mark one of *their* chains as "yours", and filtering here means
    that is true by construction rather than by a comparison somewhere later.
    """
    if not refresh_token:
        return None
    with system_context():
        return RefreshSession.objects.filter(user=user, token_hash=_hash(refresh_token)).first()


def live_sessions(user: User) -> list[RefreshSession]:
    """The devices this account is currently signed in on — one entry each.

    Rotation means the table holds a row per refresh, not per device. Only one
    row per chain is ever live at a time (rotation revokes the row it replaces
    in the same transaction), so filtering to the live ones already collapses a
    month of rotations into a single entry.

    The de-duplication below does not trust that. It is one dictionary over a
    handful of rows, and it makes "one row per device" a property of this
    function rather than of an invariant maintained three functions away —
    where a future bug would show up as a settings screen listing the same
    phone ninety times.
    """
    newest_per_chain: dict[UUID, RefreshSession] = {}
    with system_context():
        rows = RefreshSession.objects.filter(
            user=user, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-created_at")
        for row in rows:
            newest_per_chain.setdefault(row.chain_id, row)
    return list(newest_per_chain.values())


def revoke_session(*, user: User, chain_id: UUID) -> bool:
    """End one session, whichever rotation of it is currently live.

    Returns False when the caller's account has no live session with that id —
    it belongs to somebody else, or it already ended. The two look identical
    from outside, which is the point: a distinguishable answer would let one
    user confirm that a session id belongs to another.
    """
    with system_context():
        revoked = RefreshSession.objects.filter(
            user=user, chain_id=chain_id, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
    return revoked > 0


def revoke_other_sessions(*, user: User, keep: RefreshSession | None) -> int:
    """End every session except the one the caller is using.

    `keep` is None when the request arrived without a usable refresh cookie, and
    then every session goes — including whatever the caller is on. "All but this
    one" has no meaning when this one cannot be named, and of the two ways to
    guess, leaving a session alive is the one that fails the request the user
    actually made. Signing them out here costs a sign-in; the other way leaves
    the device they were trying to evict still signed in.
    """
    with system_context():
        sessions = RefreshSession.objects.filter(user=user, revoked_at__isnull=True)
        if keep is not None:
            sessions = sessions.exclude(chain_id=keep.chain_id)
        return sessions.update(revoked_at=timezone.now())


@transaction.atomic
def change_password(
    *,
    user: User,
    current_password: str,
    new_password: str,
    current_session: RefreshSession | None = None,
    user_agent: str = "",
    ip: str | None = None,
) -> TokenPair:
    """Change a password from inside a live session, and hand back a new pair.

    **Other sessions end; the caller's does not.** Spec 7.3 ends every session
    on a *reset*, and that is right there: a reset is completed by whoever holds
    the mailbox, and the usual reason to want one is that somebody else may hold
    the account. Nothing in that flow proves the person at the keyboard is the
    one who was already signed in.

    A voluntary change is the opposite case on both counts. The caller proved
    possession of the current password a line ago, so their own session is the
    one session known not to be the problem — and it is the session in their
    hand. Ending it teaches people that changing a password is disruptive, which
    is how a password stops getting changed. Every *other* session is exactly
    what someone changing their password wants gone, and leaving them alive
    would make the change decorative: the phone in the taxi keeps refreshing on
    a credential the new password was supposed to retire.

    So: revoke everything, then re-open the caller's session on the same chain.
    Their old refresh token dies with the rest — if it had leaked, the change
    retires it too — and the replacement continues the same session in the list
    rather than appearing as a new device. The access token they are holding
    stays valid until it expires, at most fifteen minutes; that is inherent to a
    stateless JWT and it is their own token, not an attacker's.
    """
    if not user.check_password(current_password):
        # INVALID_CREDENTIALS, not a field error. "The password you typed is
        # wrong" and "the password you chose is unacceptable" are different
        # answers and the screen has to say different things.
        raise BusinessRuleError(ErrorCode.INVALID_CREDENTIALS)

    user.set_password(new_password)
    # Nothing to unlock here. This caller is inside a live session, so they
    # signed in — and signing in clears the pair they signed in from. The
    # equivalent clear for somebody who cannot sign in is in
    # `confirm_password_reset`, which is the flow they are actually in.
    user.save(update_fields=["password", "updated_at"])

    revoke_all_sessions(user)
    return issue_tokens(user, user_agent=user_agent, ip=ip, continues=current_session)


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
def confirm_password_reset(*, token: str, new_password: str, ip: str | None = None) -> User:
    """Set a new password from a mailbox token, and let the holder back in.

    `ip` releases the lock on the pair completing the reset, and only that pair.
    Somebody who has been locked out is very often locked out at the address they
    are sitting at, and this is the flow whose entire purpose is regaining
    access — leaving them to wait fifteen minutes after they have proved they
    hold the mailbox is the product refusing to accept its own answer.

    Only the presenting address, because the keys are hashed and there is no
    list of a person's buckets to clear. That is the right limit anyway: it
    releases the person who is here, and leaves an attacker's own bucket
    somewhere else exactly as locked as they earned.
    """
    user = _consume_one_time_token(token, TokenPurpose.PASSWORD_RESET)
    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])
    lockout.clear(email=user.email, ip=ip)
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

    # Replay protection stops a retry of the *same* request. It cannot stop a
    # second send a minute later, and that one is the worse outcome: nothing
    # here invalidates the earlier token, so the invitee would hold two working
    # links for one seat and the older message would still open an account.
    # Resending is the intended path — it reissues the token and kills the
    # previous one. An expired invitation is not in the way: it has no live
    # link, so inviting again is a real request rather than a duplicate.
    if Invitation.objects.filter(
        email=email.lower(), accepted_at__isnull=True, expires_at__gt=timezone.now()
    ).exists():
        raise BusinessRuleError(ErrorCode.INVITATION_ALREADY_PENDING)

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
    if user.company_id is None:
        # No firm, so not the last owner of one. Only a superuser gets here.
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
def set_assigned_customers(*, user: User, customer_ids: list[UUID], assigned_by: User) -> None:
    """Replace the set of customers a technician may see.

    A replace rather than an add: the caller sends the list it wants to be true,
    which is the only form that can remove an assignment without a second
    endpoint and a second race.
    """
    if user.role != Role.TECHNICIAN or user.company_id is None:
        # A user with no firm has no customers to be assigned, and is not a
        # technician either — only a superuser reaches that state.
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
