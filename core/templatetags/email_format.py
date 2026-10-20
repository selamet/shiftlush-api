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


# Written out rather than taken from Django's `date:"F Y"`: that answers in
# whatever language is active, and a worker or a management command has no
# reason to have Turkish active. The e-mail is Turkish either way.
_TR_MONTHS = (
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
)


@register.filter
def tr_month(value: object) -> str:
    """ "Ekim 2026" from a "YYYY-MM" period, or a date in that month.

    Anything else is printed as it came, for the same reason as `tr_date`.
    """
    if isinstance(value, date):
        return f"{_TR_MONTHS[value.month - 1]} {value.year}"
    if isinstance(value, str):
        year, _, month = value.partition("-")
        if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
            return f"{_TR_MONTHS[int(month) - 1]} {year}"
    return "" if value is None else str(value)
