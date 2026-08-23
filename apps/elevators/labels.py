"""Printable QR labels.

A label lives in a machine room for years: dim light, dust, a phone held at an
angle by someone who has already climbed a ladder. That is what decides the
technical choices here — error correction level H, a 28 mm symbol, no colour —
rather than anything about how it looks on screen.

Rendering happens in the request rather than in a background job. A sheet of
twelve takes well under a second, and a job queue would mean a download the user
has to come back for, which for a print-and-walk-away task is worse than
waiting.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import qrcode
from django.conf import settings
from django.template.loader import render_to_string
from qrcode.constants import ERROR_CORRECT_H

from apps.elevators.models import Elevator

logger = logging.getLogger(__name__)

#: Twelve to an A4 sheet, 3 across and 4 down.
LABELS_PER_PAGE = 12

#: How many labels one request may ask for. A firm with 500 elevators clicking
#: "print all" would otherwise hold a worker for a minute and produce a PDF
#: nobody prints in one go.
MAX_LABELS = 240


class PdfRenderingUnavailable(RuntimeError):
    """WeasyPrint is not installed in this environment."""


@dataclass(frozen=True)
class LabelData:
    name: str
    building_name: str
    registration_number: str
    qr_image: str


def qr_data_uri(url: str) -> str:
    """A QR as an inline PNG.

    Inline rather than a file or a URL: WeasyPrint would otherwise fetch each
    one, and a PDF that depends on the network to render is a PDF that fails in
    the one place it is needed.
    """
    code = qrcode.QRCode(
        version=None,
        # Thirty per cent of the symbol can be missing and it still reads. On a
        # label that will be wiped with a greasy rag every few months, this is
        # the single most valuable setting on this page.
        error_correction=ERROR_CORRECT_H,
        box_size=10,
        border=2,
    )
    code.add_data(url)
    code.make(fit=True)

    buffer = BytesIO()
    code.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def label_url(token: str) -> str:
    """What scanning the label opens.

    Points at the frontend, not at the API: a phone camera opens a browser, and
    the browser has to decide whether the person is signed in. The token is not
    derived from the elevator's id or registration number — those are guessable,
    and a competitor could walk the sequence and scrape a customer list.
    """
    return f"{settings.FRONTEND_URL}/q/{token}"


def build_html(elevators: list[Elevator], company: Any) -> str:
    """The sheet as HTML, before it becomes a PDF.

    Separate from the render so the layout can be asserted directly — how many
    labels land on a page is a property of this markup, not of the PDF writer.
    """
    labels = [
        LabelData(
            name=elevator.name or elevator.registration_number,
            building_name=elevator.building.name,
            registration_number=elevator.registration_number,
            qr_image=qr_data_uri(label_url(elevator.qr_token)),
        )
        for elevator in elevators
    ]

    # This used to render inside `translation.override("tr")`, on the stated
    # grounds that the label is read by a person standing in Turkey. The
    # template has no `{% trans %}` in it and nothing else it renders is
    # localised, so the override forced a language onto a document that had no
    # opinion about language — a guard that would have gone on passing had the
    # template been in the wrong one. Turkish reaches the sheet the same way it
    # reaches an e-mail: written into the template. See the deviations table.
    return render_to_string(
        "labels/elevator_labels.html",
        {
            "elevators": labels,
            "company_name": company.display_name,
            "company_phone": company.phone,
            "company_logo": _logo_data_uri(company),
        },
    )


def render_labels(elevators: list[Elevator], company: Any) -> bytes:
    """One PDF holding a label for each elevator, in the order given."""
    try:
        from weasyprint import HTML
    except ImportError as exc:  # pragma: no cover - depends on the environment
        # Deliberately loud. WeasyPrint needs system libraries, so a deployment
        # that skipped the `pdf` dependency group should say so plainly rather
        # than return a broken file.
        raise PdfRenderingUnavailable(
            "WeasyPrint is not installed. Install the 'pdf' dependency group."
        ) from exc

    return bytes(HTML(string=build_html(elevators, company)).write_pdf())


def _logo_data_uri(company: Any) -> str:
    """The company logo, inlined, or nothing.

    A missing or unreadable logo must not cost anyone their labels: the sheet is
    still correct without it, and a technician standing at a lift does not care
    whose logo is in the corner.
    """
    if company.logo_id is None or not company.logo.storage_key:
        return ""

    from botocore.exceptions import BotoCoreError, ClientError

    from core import storage

    try:
        data, content_type = storage.fetch(company.logo.storage_backend, company.logo.storage_key)
    except (storage.ObjectNotFound, ClientError, BotoCoreError, OSError):
        logger.warning("Could not read the company logo for labels", exc_info=True)
        return ""
    return f"data:{content_type};base64," + base64.b64encode(data).decode()
