"""An account's statement (ekstre): on screen, as CSV, as a PDF and by mail.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. A statement is the
account's movements in a date range, each with the running balance after it,
under the balance carried in from before the range.

Every row is a movement, reversals included: a cancelled collection is two
lines that net to zero, which is what an accountant expects to see. Each row
names its source document so the web can link it.

A read: no locks. The opening is read first and the rows after it, both in the
movements' own order, so with no date bounds the closing balance is exactly
`account_balance(account)`.

Every Turkish word is in templates/ledger/ and templates/email/statement/, not
here. The CSV follows the M7 export (apps.billing.documents): UTF-8 with a BOM,
semicolons, and a decimal comma.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from django.template.loader import render_to_string

from apps.billing.documents import BOM, PdfRenderingUnavailable
from apps.companies.models import Company
from apps.ledger.balances import ZERO, account_balance_before
from apps.ledger.models import Account, Direction, Movement
from apps.reports.services import recipients_of
from apps.users.models import User
from core import mail
from core.csv_cells import safe_cell
from core.error_codes import ErrorCode
from core.exceptions import Conflict

__all__ = [
    "PdfRenderingUnavailable",
    "Statement",
    "StatementRow",
    "build_statement_html",
    "render_statement_pdf",
    "send_statement",
    "statement_csv",
    "statement_filename",
    "statement_recipients",
    "statement_rows",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatementRow:
    date: date
    description: str
    debit: Decimal
    credit: Decimal
    #: The account's balance after this row.
    balance: Decimal
    #: "app_label.model_name" of the source document, as on the movement.
    document_type: str
    document_id: uuid.UUID | None
    #: The mirror of an earlier row: half of a cancelled document.
    is_reversal: bool
    movement_id: uuid.UUID


@dataclass(frozen=True)
class Statement:
    account: Account
    start: date | None
    end: date | None
    #: The balance strictly before `start`; zero without a start.
    opening: Decimal
    rows: list[StatementRow] = field(default_factory=list)
    #: The balance after the last row: the opening when there is none.
    closing: Decimal = ZERO

    @property
    def currency(self) -> str:
        return self.account.currency

    @property
    def total_debit(self) -> Decimal:
        return sum((row.debit for row in self.rows), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((row.credit for row in self.rows), ZERO)


def statement_rows(account: Account, *, start: date | None, end: date | None) -> Statement:
    """The account's movements from `start` to `end`, both inclusive and optional."""
    opening = account_balance_before(account, start) if start is not None else ZERO
    movements = Movement.objects.filter(account=account)
    if start is not None:
        movements = movements.filter(date__gte=start)
    if end is not None:
        movements = movements.filter(date__lte=end)
    rows: list[StatementRow] = []
    balance = opening
    for movement in movements.order_by("date", "created_at", "id"):
        amount = Decimal(movement.amount).quantize(ZERO)
        debit = amount if movement.direction == Direction.DEBIT else ZERO
        credit = amount if movement.direction == Direction.CREDIT else ZERO
        balance = balance + debit - credit
        rows.append(
            StatementRow(
                date=movement.date,
                description=movement.description,
                debit=debit,
                credit=credit,
                balance=balance,
                document_type=movement.document_type,
                document_id=movement.document_id,
                is_reversal=movement.reverses_id is not None,
                movement_id=movement.pk,
            )
        )
    return Statement(
        account=account, start=start, end=end, opening=opening, rows=rows, closing=balance
    )


# CSV ------------------------------------------------------------------------------


def _decimal(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def _date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _label(key: str) -> str:
    return render_to_string("ledger/statement_label.txt", {"key": key}).strip()


def _description(row: StatementRow, reversal: str) -> str:
    return f"{reversal}: {row.description}" if row.is_reversal else row.description


def statement_csv(statement: Statement) -> str:
    """The statement as the text of a CSV file: the opening, each row, the closing."""
    header = [
        line.strip()
        for line in render_to_string("ledger/statement_header.txt").splitlines()
        if line.strip()
    ]
    reversal = _label("reversal")
    buffer = io.StringIO()
    # As the M7 export: the BOM tells Excel the file is UTF-8, and `\r\n` is
    # what Excel writes and expects.
    buffer.write(BOM)
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerow(
        [_date(statement.start), _label("opening"), "", "", _decimal(statement.opening)]
    )
    for row in statement.rows:
        writer.writerow(
            [
                _date(row.date),
                # Typed by a person: never a formula (core.csv_cells).
                safe_cell(_description(row, reversal)),
                _decimal(row.debit),
                _decimal(row.credit),
                _decimal(row.balance),
            ]
        )
    writer.writerow(
        [
            _date(statement.end),
            _label("closing"),
            _decimal(statement.total_debit),
            _decimal(statement.total_credit),
            _decimal(statement.closing),
        ]
    )
    return buffer.getvalue()


# HTML and PDF ---------------------------------------------------------------------


def build_statement_html(statement: Statement, *, company: Company) -> str:
    """The PDF's page as HTML. Autoescaped: a description is whatever was typed."""
    return render_to_string("ledger/statement.html", {"statement": statement, "company": company})


def render_statement_pdf(statement: Statement, *, company: Company) -> bytes:
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on the environment
        raise PdfRenderingUnavailable(
            "WeasyPrint is not available. Install the 'pdf' dependency group and Pango."
        ) from exc
    return bytes(HTML(string=build_statement_html(statement, company=company)).write_pdf())


def statement_filename(statement: Statement, extension: str) -> str:
    """`ekstre-<start>-<end>.<extension>`, each bound left out when open."""
    parts = [_label("filename")]
    parts += [day.isoformat() for day in (statement.start, statement.end) if day is not None]
    return f"{'-'.join(parts)}.{extension}"


# Mail -----------------------------------------------------------------------------


def statement_recipients(account: Account) -> list[str]:
    """The customer's contacts who receive reports, with an address.

    Only a customer's account has them; a supplier's or a staff member's has
    none, and is refused as a customer without one is.
    """
    customer = account.customer
    if customer is None:
        return []
    return recipients_of(customer)


def send_statement(
    account: Account, *, start: date | None, end: date | None, by: User | None
) -> list[str]:
    """E-mail the statement's PDF to each recipient, after the transaction commits.

    Raises `409 REPORT_NO_RECIPIENT` when there is nobody to send it to, before
    anything is rendered. `by` is who chose to send it, for the log; the mail
    itself does not name them.
    """
    emails = statement_recipients(account)
    if not emails:
        raise Conflict(ErrorCode.REPORT_NO_RECIPIENT)
    company = Company.objects.get(pk=account.company_id)
    statement = statement_rows(account, start=start, end=end)
    pdf = render_statement_pdf(statement, company=company)
    context: dict[str, Any] = {
        "company_name": company.display_name,
        "account_name": account.name,
        "start": start,
        "end": end,
        "closing": statement.closing,
        "currency": statement.currency,
    }
    filename = statement_filename(statement, "pdf")
    for email in emails:
        mail.send_statement(to=email, context=context, pdf=pdf, filename=filename)
    logger.info(
        "Statement of account %s queued for %d recipient(s) by %s",
        account.pk,
        len(emails),
        getattr(by, "pk", None),
    )
    return emails
