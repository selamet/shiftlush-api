"""Outgoing e-mail.

Three e-mails exist in phase 1 — an invitation, a password reset and an address
verification — and every one of them carries a single-use link. That shapes the
rules here:

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
