"""Raising notifications, and the per-user e-mail preferences.

`notify()` is the only way a notification comes into existence. It is written
for callers that run unattended — the M2 jobs run daily, across every company,
and may run twice — so it is idempotent by `dedupe_key` and never sends mail
for work that was rolled back.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any, NotRequired, TypedDict

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction

from apps.notifications.models import (
    EMAIL_ALWAYS,
    EMAIL_DEFAULTS,
    Notification,
    NotificationKind,
    NotificationPreference,
)
from apps.notifications.tasks import send_notification_email
from apps.users.models import User
from core import mail


class PreferenceEntry(TypedDict):
    kind: str
    email: bool
    #: Output only: the kind is in EMAIL_ALWAYS and its e-mail cannot be turned off.
    locked: NotRequired[bool]


def normalize_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """`data` as it will be stored: plain JSON, dates as ISO strings.

    The callers hold model values — `contract.end_date` is a `date`, an id is a
    `UUID` — and the JSONField's plain encoder refuses both at insert time. They
    are converted once, here, so what the client and the e-mail template read
    back is exactly what was stored. Anything with no JSON form — a model
    instance passed by mistake — is refused rather than stringified into
    something nobody can render.
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"Notification data must be a mapping, not {type(data).__name__}.")
    try:
        normalized: dict[str, Any] = json.loads(
            json.dumps(dict(data), cls=DjangoJSONEncoder, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Notification data must be JSON-serializable: {exc}") from exc
    return normalized


def notify(
    *,
    kind: str,
    recipients: Iterable[User],
    subject_type: str,
    subject_id: uuid.UUID,
    data: Mapping[str, Any],
    dedupe_key: str,
) -> list[Notification]:
    """Raise `kind` for each recipient, once, and return the ones created.

    - A recipient who already holds `dedupe_key` is skipped. The unique
      constraint decides, not a read beforehand: two runs of the same job racing
      each other would both pass a read, and the loser would raise a duplicate.
      Each insert gets its own savepoint, because on PostgreSQL a failed insert
      otherwise poisons the caller's whole transaction.
    - Inactive and deleted users are skipped; nobody reads them.
    - The company comes from the recipient, never from the context. That is what
      lets a job call this under ``system_context()`` across every firm, while a
      caller inside one firm's context still cannot raise a notification for a
      user of another — CompanyOwnedModel.save() refuses it.
    - E-mail is enqueued only after the surrounding transaction commits, so a
      job that fails halfway and rolls back has told nobody anything.
    """
    if kind not in NotificationKind.values:
        raise ValueError(f"Unknown notification kind: {kind!r}")
    payload = normalize_data(data)

    created: list[Notification] = []
    for recipient in recipients:
        company_id = recipient.company_id
        if not recipient.is_active or recipient.is_deleted or company_id is None:
            continue
        notification = _create_once(
            company_id=company_id,
            recipient=recipient,
            kind=kind,
            subject_type=subject_type,
            subject_id=subject_id,
            data=payload,
            dedupe_key=dedupe_key,
        )
        if notification is not None:
            created.append(notification)

    # Inside core.mail.suppressed() — the demo seeding — the in-app
    # notifications are raised and nothing is e-mailed.
    emailed = (
        set() if mail.is_suppressed() else _wants_email({n.recipient_id for n in created}, kind)
    )
    for notification in created:
        if notification.recipient_id in emailed:
            # robust: one notification failing to enqueue (the queue table
            # briefly unavailable) must not stop the others, nor surface as an
            # error in a job whose real work has already committed. Django logs
            # the failure.
            transaction.on_commit(
                partial(send_notification_email.enqueue, str(notification.pk)), robust=True
            )
    return created


def _create_once(
    *,
    company_id: uuid.UUID,
    recipient: User,
    kind: str,
    subject_type: str,
    subject_id: uuid.UUID,
    data: dict[str, Any],
    dedupe_key: str,
) -> Notification | None:
    try:
        with transaction.atomic():
            return Notification.objects.create(
                company_id=company_id,
                recipient=recipient,
                kind=kind,
                subject_type=subject_type,
                subject_id=subject_id,
                data=data,
                dedupe_key=dedupe_key,
            )
    except IntegrityError:
        # Only the dedupe constraint is expected to fire here. Anything else is
        # a real error and is not swallowed: the read is by recipient and key,
        # so it needs no tenant context and crosses no tenant boundary.
        duplicate = Notification.unscoped.filter(
            recipient=recipient, dedupe_key=dedupe_key, is_deleted=False
        ).exists()
        if duplicate:
            return None
        raise


def _wants_email(user_ids: set[uuid.UUID], kind: str) -> set[uuid.UUID]:
    """The users among `user_ids` whose preference, or the default, says e-mail."""
    if not user_ids:
        return set()
    if kind in EMAIL_ALWAYS:
        return set(user_ids)
    # `unscoped` with an explicit user filter: these are the recipients' own
    # rows, and the tenant manager would return nothing at all when notify() is
    # called with no company in context — silently replacing every user's
    # choice with the default.
    explicit = dict(
        NotificationPreference.unscoped.filter(
            user_id__in=user_ids, kind=kind, is_deleted=False
        ).values_list("user_id", "email")
    )
    default = EMAIL_DEFAULTS[kind]
    return {user_id for user_id in user_ids if explicit.get(user_id, default)}


def preferences_for(user: User) -> list[PreferenceEntry]:
    """Every kind with the user's e-mail flag, defaults filled in, in enum order."""
    explicit = dict(NotificationPreference.objects.filter(user=user).values_list("kind", "email"))
    return [
        {
            "kind": kind,
            "email": kind in EMAIL_ALWAYS or explicit.get(kind, EMAIL_DEFAULTS[kind]),
            "locked": kind in EMAIL_ALWAYS,
        }
        for kind in NotificationKind.values
    ]


@transaction.atomic
def set_preferences(*, user: User, entries: Iterable[PreferenceEntry]) -> list[PreferenceEntry]:
    """Replace the user's flags with exactly what was sent.

    A kind that is sent is stored as a row even when it equals the default, so
    an explicit choice survives a later change of the default. A kind left out
    goes back to the default: its row is soft-deleted, which the audit trail
    records like any other removal.
    """
    company_id = user.company_id
    if company_id is None:
        # A superuser made with createsuperuser has no firm, and so nowhere to
        # file a preference. The view refuses them before this point.
        raise ValueError("A user without a company has no notification preferences.")
    chosen = {entry["kind"]: entry["email"] for entry in entries}
    existing = {row.kind: row for row in NotificationPreference.objects.filter(user=user)}

    for kind in NotificationKind.values:
        row = existing.get(kind)
        if kind in chosen:
            if row is None:
                NotificationPreference.objects.create(
                    company_id=company_id,
                    user=user,
                    kind=kind,
                    email=chosen[kind],
                    created_by=user,
                    updated_by=user,
                )
            elif row.email != chosen[kind]:
                row.email = chosen[kind]
                row.updated_by = user
                row.save(update_fields=["email", "updated_by", "updated_at"])
        elif row is not None:
            row.updated_by = user
            row.save(update_fields=["updated_by", "updated_at"])
            row.delete()

    return preferences_for(user)
