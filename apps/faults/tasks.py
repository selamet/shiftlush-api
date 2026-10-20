"""Fault jobs, run daily (core/scheduling.py).

M4: docs/superpowers/specs/2026-10-20-m4-fault-management-design.md.
"""

from __future__ import annotations

from django.tasks import task

from apps.faults.services import forget_public_reporters
from core.context import system_context


@task
def purge_public_reporter_data() -> int:
    """Blank public reporters' names and phones 90 days after their fault ends.

    Across every firm, like the other daily jobs: there is no request to take a
    tenant from. Idempotent — a blanked row no longer matches.
    """
    with system_context():
        return forget_public_reporters()
