"""The ledger's daily job (core/scheduling.py).

F0: docs/superpowers/specs/2026-10-23-f0-ledger-design.md.
"""

from __future__ import annotations

from django.tasks import task

from apps.ledger import migration_helpers


@task
def carry_over_invoices() -> dict[str, int]:
    """Post every issued or paid draft the ledger has not posted yet.

    The carry-over migration posts every draft that exists when it runs; an
    old container still serving during the deploy can issue or mark paid one
    after it, the M7 way, with no posting. This posts those, and anything else
    that slipped past. Idempotent: a draft already carried is not found again.
    A draft that fails is logged and counted, and retried the next day.
    """
    run = migration_helpers.carry_over_invoices()
    return {"posted": run.posted, "collected": run.collected, "failed": run.failed}
