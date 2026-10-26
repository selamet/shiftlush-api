"""The two files a draft leaves the product as: a CSV row and a PDF page.

Both are for a Turkish accountant. The CSV is semicolon-separated UTF-8 with a
byte-order mark, which is what Excel in a Turkish locale needs to open it with
the columns split and the letters right, and its numbers use a decimal comma.
The PDF is rendered with WeasyPrint like the QR labels, and says it is a draft
until the draft is issued.

The column names, status labels and every other Turkish word are in templates
(templates/billing/), not here.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.template.loader import render_to_string

from apps.billing.models import InvoiceDraft, InvoiceStatus
from core.csv_cells import safe_cell

BOM = "﻿"


class PdfRenderingUnavailable(RuntimeError):
    """WeasyPrint, or the system libraries under it, is not available here."""


def _decimal(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def _date(value: Any) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def export_csv(drafts: Iterable[InvoiceDraft]) -> str:
    """One row per draft, under a header row, as the text of a CSV file."""
    header = [
        line.strip()
        for line in render_to_string("billing/export_header.txt").splitlines()
        if line.strip()
    ]
    buffer = io.StringIO()
    # Excel reads the BOM to know the file is UTF-8; without it every Turkish
    # letter in the header and the names arrives as mojibake. `\r\n` is what
    # Excel writes and expects.
    buffer.write(BOM)
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    labels: dict[str, str] = {}
    for draft in drafts:
        if draft.status not in labels:
            labels[draft.status] = render_to_string(
                "billing/status_label.txt", {"status": draft.status}
            ).strip()
        writer.writerow(
            [
                # Typed by a person: never a formula (core.csv_cells).
                safe_cell(draft.customer.legal_name),
                safe_cell(draft.contract.contract_number),
                _date(draft.period_start),
                _date(draft.period_end),
                _decimal(draft.subtotal),
                _decimal(draft.vat_amount),
                _decimal(draft.total),
                draft.currency,
                labels[draft.status],
                safe_cell(draft.invoice_number),
            ]
        )
    return buffer.getvalue()


def build_html(draft: InvoiceDraft) -> str:
    """The PDF's page as HTML: asserted directly in the tests, like the labels."""
    return render_to_string(
        "billing/invoice_draft.html",
        {
            "draft": draft,
            "lines": list(draft.lines.all()),
            "company": draft.company,
            "issued": draft.status in (InvoiceStatus.ISSUED, InvoiceStatus.PAID),
            "cancelled": draft.status == InvoiceStatus.CANCELLED,
        },
    )


def render_pdf(draft: InvoiceDraft) -> bytes:
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on the environment
        # OSError too: WeasyPrint imports but cannot load Pango where the system
        # libraries are missing, and that is the same "not here" to the caller.
        raise PdfRenderingUnavailable(
            "WeasyPrint is not available. Install the 'pdf' dependency group and Pango."
        ) from exc
    return bytes(HTML(string=build_html(draft)).write_pdf())
