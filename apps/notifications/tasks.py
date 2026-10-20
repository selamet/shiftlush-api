from __future__ import annotations

from django.tasks import task

from apps.notifications.models import Notification
from core.mail import send_notification


@task
def send_notification_email(notification_id: str) -> None:
    """E-mail one notification to its recipient.

    A task rather than a send inside notify(), so a slow mail server never
    holds a job or a request. It takes the id, not the row: the queue stores
    JSON, and the row may have changed between enqueue and run.

    Read through `unscoped` by primary key. A worker has no tenant context, and
    the id was handed over by notify() itself, so there is no boundary to
    cross — `system_context()` would be a wider door than this needs.
    """
    notification = (
        Notification.unscoped.select_related("recipient")
        .filter(pk=notification_id, is_deleted=False)
        .first()
    )
    if notification is None:
        return
    recipient = notification.recipient
    # Deactivated between enqueue and run: the person has left the firm, and
    # mail about its contracts is no longer theirs to receive.
    if not recipient.is_active or recipient.is_deleted:
        return
    send_notification(
        to=recipient.email,
        first_name=recipient.first_name,
        kind=notification.kind,
        subject_type=notification.subject_type,
        subject_id=str(notification.subject_id),
        data=notification.data,
    )
