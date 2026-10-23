"""The monthly customer report job, run daily (core/scheduling.py).

Design: docs/superpowers/specs/2026-10-22-monthly-customer-report-design.md.
"""

from __future__ import annotations

from datetime import timedelta

from django.tasks import task

from apps.companies.models import Company
from apps.reports.services import RunResult, generate
from core.context import system_context
from core.scheduling import local_today

#: A report is e-mailed only this long after its month ended. A run that was
#: missed catches up, but a firm should not get October's report in December.
MAIL_WINDOW = timedelta(days=7)


@task
def monthly_customer_reports() -> dict[str, int]:
    """Make last month's missing reports for every firm.

    Under the system context, like the other daily jobs. Idempotent through the
    unique (customer, period) on live reports: a second run makes nothing and
    sends nothing.
    """
    today = local_today()
    month_end = today.replace(day=1) - timedelta(days=1)
    period = f"{month_end.year:04d}-{month_end.month:02d}"
    send = today - month_end <= MAIL_WINDOW
    total = RunResult()
    with system_context():
        for company in Company.objects.filter(is_active=True):
            total.add(generate(company, period, today=today, send=send))
    return total.as_dict()
