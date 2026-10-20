"""Background tasks that belong to infrastructure rather than to one app."""

from __future__ import annotations

from django.core.management import call_command
from django.tasks import task, task_backends

from core.idempotency import purge_expired_keys


@task
def purge_idempotency_keys() -> int:
    return purge_expired_keys()


@task
def prune_task_results() -> None:
    """Drop finished task results: successes after 7 days, failures after 30.

    Failures are kept longer because they are the ones somebody comes looking
    for. Only the database backend stores results; on the immediate backend
    used locally and in tests there is nothing to prune.
    """
    from django_tasks_db import DatabaseBackend

    if not isinstance(task_backends["default"], DatabaseBackend):
        return
    call_command("prune_db_task_results", min_age_days=7, failed_min_age_days=30)
