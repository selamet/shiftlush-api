"""Outgoing e-mail.

Three e-mails exist in phase 1 — an invitation, a password reset and an address
verification — and every one of them carries a single-use link. M2 adds a
fourth, the e-mail copy of an in-app notification. That shapes the rules here:

  - A send failure never rolls back the thing it announces. Mail goes out after
    the transaction commits, so a mail server that is briefly down cannot undo
    an invitation the administrator has already been told was created.
  - A send failure never fails the request either. The administrator can resend;
    a 500 on an otherwise successful invitation would have them create a second
    one, and now two live tokens exist for one seat.
  - The Turkish lives in templates, not here. The API returns codes, not
    sentences, and this is the one part of the backend that legitimately talks
    to a person.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send(*, template: str, subject_key: str, to: str, context: dict[str, Any]) -> None:
    # These three renders used to sit inside `translation.override("tr")`, so
    # that a recipient got Turkish regardless of what a management command had
    # last activated. There is nothing for it to act on: the templates hold
    # their Turkish literally, with no `{% trans %}` and no `{% load i18n %}`,
    # and the only values they interpolate are names, URLs and a small integer
    # that reads the same in every locale. The override was a guard that could
    # not fail and could not have caught an English e-mail either. See the
    # deviations table for why the templates are written this way.
    subject = render_to_string(f"email/{template}/subject.txt", context).strip()
    text_body = render_to_string(f"email/{template}/body.txt", context)
    html_body = render_to_string(f"email/{template}/body.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
    )
    message.attach_alternative(html_body, "text/html")

    try:
        message.send(fail_silently=False)
    except Exception:
        # Logged rather than raised: the caller has already committed, and a
        # 500 here would make an administrator create a second invitation for
        # the same seat, leaving two live tokens.
        logger.exception("Could not send %s to %s", template, to, extra={"subject": subject_key})


def send_after_commit(**kwargs: object) -> None:
    """Queue a send for after the current transaction commits.

    Outside a transaction this sends immediately, which is what the tests and
    management commands want.
    """
    transaction.on_commit(lambda: _send(**kwargs))  # type: ignore[arg-type]


def send_invitation(*, to: str, first_name: str, company_name: str, token: str) -> None:
    send_after_commit(
        template="invitation",
        subject_key="invitation",
        to=to,
        context={
            "first_name": first_name,
            "company_name": company_name,
            # The plaintext token exists exactly once, here. It is never stored,
            # never shown to the administrator and never chosen by them.
            "url": f"{settings.FRONTEND_URL}/invitation/{token}",
            "valid_for_hours": 72,
        },
    )


def send_password_reset(*, to: str, first_name: str, token: str) -> None:
    send_after_commit(
        template="password_reset",
        subject_key="password_reset",
        to=to,
        context={
            "first_name": first_name,
            "url": f"{settings.FRONTEND_URL}/password-reset/{token}",
            "valid_for_hours": 1,
        },
    )


def send_email_verification(*, to: str, first_name: str, token: str) -> None:
    send_after_commit(
        template="email_verification",
        subject_key="email_verification",
        to=to,
        context={
            "first_name": first_name,
            "url": f"{settings.FRONTEND_URL}/verify-email/{token}",
            "valid_for_hours": 24,
        },
    )


def send_portal_invitation(
    *,
    to: str,
    full_name: str,
    company_name: str,
    customer_name: str,
    token: str,
    valid_for_hours: int,
) -> None:
    """Invite a customer contact to the portal (M6). Its own route tree."""
    send_after_commit(
        template="portal_invitation",
        subject_key="portal_invitation",
        to=to,
        context={
            "full_name": full_name,
            "company_name": company_name,
            "customer_name": customer_name,
            "url": f"{settings.FRONTEND_URL}/portal/accept/{token}",
            "valid_for_hours": valid_for_hours,
        },
    )


def send_portal_password_reset(*, to: str, full_name: str, token: str) -> None:
    send_after_commit(
        template="portal_password_reset",
        subject_key="portal_password_reset",
        to=to,
        context={
            "full_name": full_name,
            "url": f"{settings.FRONTEND_URL}/portal/password-reset/{token}",
            "valid_for_hours": 1,
        },
    )


#: Where a notification's button leads, by its subject type. A type missing here
#: still gets a working link — to the notification list — rather than a guess.
NOTIFICATION_SUBJECT_PATHS = {
    "contract": "contracts",
    "elevator": "elevators",
    "fault": "faults",
}

#: Subjects that are a screen rather than a record, so the link carries no id.
#: "plan" is a technician's work list, which is the app's landing screen for
#: them (M3) — the root is where it lives.
NOTIFICATION_SUBJECT_SCREENS = {
    "plan": "",
}


def notification_url(*, subject_type: str, subject_id: str) -> str:
    screen = NOTIFICATION_SUBJECT_SCREENS.get(subject_type)
    if screen is not None:
        return f"{settings.FRONTEND_URL}/{screen}"
    path = NOTIFICATION_SUBJECT_PATHS.get(subject_type)
    if path is None:
        return f"{settings.FRONTEND_URL}/notifications"
    return f"{settings.FRONTEND_URL}/{path}/{subject_id}"


def send_notification(
    *,
    to: str,
    first_name: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    data: dict[str, Any],
) -> None:
    """E-mail one in-app notification.

    Sent now rather than after commit: the caller is the notification task,
    which notify() only enqueues once its transaction has committed, so there
    is nothing left to wait for.

    One shell for every kind (`email/notification/`) and two partials per kind
    under `email/notification/kinds/` — the subject line and the sentence — so a
    new kind is two new files and no code. `data` is spread into the context so
    the partials read `{{ contract_number }}` directly; the keys set here come
    after it, so a fact named `url` cannot redirect the button.
    """
    _send(
        template="notification",
        subject_key=f"notification.{kind}",
        to=to,
        context={
            **data,
            "first_name": first_name,
            "kind": kind,
            "subject_type": subject_type,
            "url": notification_url(subject_type=subject_type, subject_id=subject_id),
            "kind_subject_template": f"email/notification/kinds/{kind}_subject.txt",
            "kind_body_template": f"email/notification/kinds/{kind}_body.txt",
        },
    )
