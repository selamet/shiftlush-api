"""Making, keeping and sending the monthly customer report.

Design: docs/superpowers/specs/2026-10-22-monthly-customer-report-design.md,
"The job". What a report says lives in `content`, how it looks in `documents`;
this is the part that stores it, records it and e-mails it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from functools import partial

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.attachments.services import save_bytes
from apps.companies.models import Company
from apps.customers.models import Customer
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.portal.models import PortalAccount
from apps.reports import documents
from apps.reports.content import ReportData, build_report
from apps.reports.models import CustomerReport, ReportDelivery
from apps.users.models import Role, User
from core import mail, storage
from core.error_codes import ErrorCode
from core.exceptions import Conflict

logger = logging.getLogger(__name__)

_OFFICE = (Role.OWNER, Role.ADMIN)


@dataclass
class RunResult:
    created: int = 0
    sent: int = 0
    no_recipient: int = 0
    not_enabled: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def add(self, other: RunResult) -> None:
        self.created += other.created
        self.sent += other.sent
        self.no_recipient += other.no_recipient
        self.not_enabled += other.not_enabled
        self.failed += other.failed


def _filename(period: str) -> str:
    return f"maintenance-report-{period}.pdf"


def _store_pdf(customer: Customer, data: ReportData, stored: list[Attachment]) -> Attachment:
    """Render and store the PDF. The object goes into `stored` at once, so a
    caller whose transaction then rolls back can remove it from the bucket."""
    attachment = save_bytes(
        company_id=customer.company_id,
        object_type=ObjectType.CUSTOMER,
        object_id=customer.pk,
        category=AttachmentCategory.MONTHLY_REPORT,
        data=documents.render_pdf(data),
        mime_type="application/pdf",
        original_filename=_filename(data.period),
        uploaded_by=None,
    )
    stored.append(attachment)
    return attachment


def _discard(stored: list[Attachment]) -> None:
    """Remove objects whose rows were rolled back, so they are not orphaned."""
    for attachment in stored:
        try:
            storage.delete(attachment.storage_backend, attachment.storage_key)
        except Exception:
            logger.exception("Could not remove the orphaned object %s", attachment.storage_key)


def _summary(data: ReportData) -> dict[str, object]:
    return {**data.summary.as_json(), "lead": asdict(data.lead)}


def recipients_of(customer: Customer) -> list[str]:
    """The addresses ticked to receive the report.

    A ticked contact can lose its address through other write paths, so a
    blank one is skipped rather than trusted.
    """
    emails = customer.contacts.filter(receives_monthly_report=True).values_list("email", flat=True)
    return sorted({email for email in emails if email})


def _report_exists(customer: Customer, period: str) -> bool:
    return CustomerReport.objects.filter(customer=customer, period=period).exists()


def generate_one(customer: Customer, period: str, *, today: date) -> CustomerReport | None:
    """Make and store the report of one customer's month.

    None when there is nothing to make: the customer had no covered lift in the
    month, or a live report for the month exists already.
    """
    stored: list[Attachment] = []
    try:
        with transaction.atomic():
            return _create(customer, period, today, stored)
    except Exception:
        _discard(stored)
        raise


def _create(
    customer: Customer, period: str, today: date, stored: list[Attachment]
) -> CustomerReport | None:
    if _report_exists(customer, period):
        return None
    data = build_report(customer, period, today=today)
    if not data.lifts:
        return None
    return CustomerReport.objects.create(
        company_id=customer.company_id,
        customer=customer,
        period=period,
        file=_store_pdf(customer, data, stored),
        generated_at=timezone.now(),
        summary=_summary(data),
    )


_FAILED_MARK = "failed: "


def _mark_failed(report_id: uuid.UUID, address: str) -> None:
    """Record that `address` did not get the mail, keeping the earlier ones."""
    row = CustomerReport.unscoped.get(pk=report_id)
    failed = row.error.split(_FAILED_MARK, 1)[1].split(", ") if _FAILED_MARK in row.error else []
    if address not in failed:
        failed.append(address)
    lead = "Partially sent" if len(failed) < len(row.recipients) else "Not sent"
    CustomerReport.unscoped.filter(pk=report_id).update(
        delivery=ReportDelivery.FAILED,
        error=f"{lead}; {_FAILED_MARK}{', '.join(failed)}",
        updated_at=timezone.now(),
    )


def _record_failure(report_id: uuid.UUID, address: str, reason: str) -> None:
    """The callback of a failed send. It runs on commit, among other sends, so
    it must not raise: a record that cannot be written is logged instead."""
    try:
        _mark_failed(report_id, address)
    except Exception:
        logger.exception("Could not record that %s failed for report %s", address, report_id)


def send_report(report: CustomerReport, *, by: User | None) -> CustomerReport:
    """E-mail the report to the customer's ticked contacts, now.

    One message per recipient with the PDF attached, after the transaction
    commits. A send that fails marks the report failed with its reason; it is
    not retried. `by` is the person who chose to send it, None for the job.
    """
    emails = recipients_of(report.customer)
    if not emails:
        raise Conflict(ErrorCode.REPORT_NO_RECIPIENT)
    assert report.file is not None
    pdf, _ = _read_file(report.file)
    portal = set(
        PortalAccount.objects.filter(customer=report.customer, is_active=True).values_list(
            "email", flat=True
        )
    )
    portal_url = f"{settings.FRONTEND_URL.rstrip('/')}/portal"
    for email in emails:
        mail.send_monthly_report(
            to=email,
            context={
                "company_name": report.company.display_name,
                "customer_name": report.customer.legal_name,
                "period": report.period,
                "lead": report.summary.get("lead", {}),
                "summary": report.summary,
                "portal_url": portal_url if email.lower() in portal else None,
            },
            pdf=pdf,
            filename=_filename(report.period),
            on_failure=partial(_record_failure, report.pk, email),
        )
    # SENT is committed before the mails go out (on commit). If the process dies
    # in between, the report reads SENT and nobody got it; the office can resend.
    report.delivery = ReportDelivery.SENT
    report.sent_at = timezone.now()
    report.recipients = emails
    report.error = ""
    report.save(update_fields=["delivery", "sent_at", "recipients", "error", "updated_at"])
    return report


def _read_file(attachment: Attachment) -> tuple[bytes, str]:
    body, content_type = storage.fetch(attachment.storage_backend, attachment.storage_key)
    return body, content_type or attachment.mime_type


@transaction.atomic
def regenerate(report: CustomerReport, *, today: date) -> CustomerReport:
    """Render the report again from the current record and replace its file.

    The old attachment is soft-deleted, not removed, so the audit trail still
    reaches it. Nothing is sent.
    """
    data = build_report(report.customer, report.period, today=today)
    old = report.file
    stored: list[Attachment] = []
    try:
        report.file = _store_pdf(report.customer, data, stored)
    except Exception:
        _discard(stored)
        raise
    report.generated_at = timezone.now()
    report.summary = _summary(data)
    report.save(update_fields=["file", "generated_at", "summary", "updated_at"])
    if old is not None:
        old.delete()
    return report


def _deliver(report: CustomerReport, company: Company, *, send: bool, result: RunResult) -> None:
    if not send or not company.monthly_reports_enabled:
        report.delivery = ReportDelivery.NOT_ENABLED
        report.save(update_fields=["delivery", "updated_at"])
        result.not_enabled += 1
    elif not recipients_of(report.customer):
        report.delivery = ReportDelivery.NO_RECIPIENT
        report.save(update_fields=["delivery", "updated_at"])
        result.no_recipient += 1
    else:
        send_report(report, by=None)
        result.sent += 1


def generate(company: Company, period: str, *, today: date, send: bool) -> RunResult:
    """Make the period's missing reports for one firm, and tell its office.

    Each customer is its own transaction, so one failure leaves the rest alone
    and leaves no row of its own: the next run tries it again. `send` is False
    for a month that ended too long ago to be worth an e-mail.
    """
    result = RunResult()
    for customer in Customer.objects.filter(company=company).order_by("id"):
        stored: list[Attachment] = []
        try:
            with transaction.atomic():
                report = _create(customer, period, today, stored)
                if report is None:
                    continue
                _deliver(report, company, send=send, result=result)
                result.created += 1
        except IntegrityError:
            _discard(stored)
            if _report_exists(customer, period):
                # Another run made it first; nothing for this one to do or report.
                continue
            logger.exception("Monthly report for customer %s, %s, failed", customer.pk, period)
            result.failed += 1
        except Exception:
            _discard(stored)
            logger.exception("Monthly report for customer %s, %s, failed", customer.pk, period)
            result.failed += 1
    if result.created or result.failed:
        notify(
            kind=NotificationKind.MONTHLY_REPORTS_READY,
            recipients=User.objects.filter(company=company, role__in=_OFFICE, is_active=True),
            subject_type="reports",
            subject_id=company.pk,
            data={
                "period": period,
                "count": result.created,
                "sent": result.sent,
                "no_recipient": result.no_recipient,
                "not_enabled": result.not_enabled,
                "failed": result.failed,
            },
            dedupe_key=f"monthly_reports_ready:{company.pk}:{period}:{today.isoformat()}",
        )
    return result
