"""The billing job, run daily (core/scheduling.py).

M7: docs/superpowers/specs/2026-10-21-m7-billing-drafts-design.md.
"""

from __future__ import annotations

from django.tasks import task

from apps.billing.services import generate
from core.context import system_context
from core.scheduling import local_today


@task
def draft_invoices() -> dict[str, int]:
    """Draft every billing period that ended yesterday or earlier.

    Across every firm under the system context, like the other daily jobs; every
    draft carries its contract's company. Idempotent through the unique
    (contract, period start) on live drafts.
    """
    with system_context():
        run = generate(today=local_today())
    return {"created": len(run.created), "vat_missing": run.vat_missing}
