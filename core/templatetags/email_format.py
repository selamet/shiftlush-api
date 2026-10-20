"""Formatting for values the e-mail templates print.

Notification `data` is JSON, so a date arrives in a template as the ISO string
it was stored as. A Turkish reader expects 30.11.2026, and the built-in `date`
filter only formats `date` objects — it prints nothing for a string.
"""

from __future__ import annotations

from datetime import date, datetime

from django import template
from django.utils import timezone

from core.scheduling import LOCAL_TZ

register = template.Library()


@register.filter
def tr_date(value: object) -> str:
    """DD.MM.YYYY from a date, a datetime or an ISO string.

    A datetime is shown on its Istanbul date, the calendar the firm works in.
    Anything unparseable is printed as it came, which is better in a sent
    e-mail than an empty gap in the middle of a sentence.
    """
    if value is None or value == "":
        return ""
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = _local_date(value)
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            try:
                parsed = _local_date(datetime.fromisoformat(value))
            except ValueError:
                parsed = None
    if parsed is None:
        return str(value)
    return parsed.strftime("%d.%m.%Y")


def _local_date(value: datetime) -> date:
    if timezone.is_aware(value):
        return value.astimezone(LOCAL_TZ).date()
    return value.date()
