"""The monthly report as a page and as a PDF.

The page is one template (templates/reports/monthly.html) over the plain data
of `apps.reports.content`; every Turkish word is there or in
core/data/report_labels.json. Nothing here queries the month: the only reads
are the logo and signature images, through storage.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping

from django.template.loader import render_to_string

from apps.attachments.models import Attachment
from apps.billing.documents import PdfRenderingUnavailable
from apps.reports.content import ReportData
from core import storage

__all__ = ["PdfRenderingUnavailable", "build_html", "fetch_images", "render_pdf"]

logger = logging.getLogger(__name__)


def build_html(data: ReportData, *, images: Mapping[str, str]) -> str:
    """The report's page. `images` maps an attachment id to a `data:` URI."""
    return render_to_string("reports/monthly.html", {"report": data, "images": images})


def fetch_images(data: ReportData) -> dict[str, str]:
    """The logo and signature thumbnails as `data:` URIs, by attachment id.

    One that cannot be read is left out and logged: the page then shows the
    name alone, and the report is still made.
    """
    wanted = {
        str(line.signature_attachment_id)
        for lift in data.lifts
        for line in lift.visits
        if line.signature_attachment_id
    }
    if data.logo_attachment_id:
        wanted.add(data.logo_attachment_id)
    images: dict[str, str] = {}
    for attachment in Attachment.unscoped.filter(pk__in=wanted):
        try:
            body, content_type = storage.fetch(attachment.storage_backend, attachment.storage_key)
        except Exception:
            logger.warning("report image %s could not be read", attachment.pk, exc_info=True)
            continue
        encoded = base64.b64encode(body).decode("ascii")
        images[str(attachment.pk)] = f"data:{content_type};base64,{encoded}"
    return images


def render_pdf(data: ReportData) -> bytes:
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on the environment
        raise PdfRenderingUnavailable(
            "WeasyPrint is not available. Install the 'pdf' dependency group and Pango."
        ) from exc
    html = build_html(data, images=fetch_images(data))
    return bytes(HTML(string=html).write_pdf())
