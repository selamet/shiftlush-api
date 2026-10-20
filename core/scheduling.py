"""What runs on a schedule, and the tick that starts it.

The schedule lives here rather than in a crontab on the server: a job that only
exists in a deployment is one nobody reviews, and the two purges this replaces
sat unscheduled for exactly that reason ("Scheduling belongs to the
deployment… scheduling is the only thing left to decide").

`run_scheduler` calls `enqueue_due` once a minute. Each run is claimed by
inserting a `ScheduledRun` for (job, local date) under a unique constraint and
enqueued in the same transaction, so a restart, a second scheduler or two ticks
in one minute cannot start a job twice — and an enqueue that fails takes its
claim with it, so the next tick tries again.

See docs/superpowers/specs/2026-10-20-background-jobs-design.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.tasks import Task
from django.utils import timezone

from core.models import ScheduledRun

# The only market the product is sold in. TIME_ZONE stays UTC: this is the
# wall clock a schedule is written in, not the one timestamps are stored in.
LOCAL_TZ = ZoneInfo("Europe/Istanbul")


def local_today() -> date:
    """Today's date where the product is used — what "overdue" and "ended" mean."""
    return timezone.now().astimezone(LOCAL_TZ).date()


@dataclass(frozen=True)
class DailySchedule:
    """Run `task` once per local day, at or after `at`.

    A scheduler that was down at `at` runs the job when it comes back the same
    day. A whole missed day is not replayed: every job here works off "as of
    now", so a second run would do nothing the first did not.
    """

    name: str
    task: Task[..., Any]
    at: time

    def is_due(self, local_now: datetime) -> bool:
        return local_now.time() >= self.at

    def period(self, local_now: datetime) -> str:
        return local_now.date().isoformat()


def get_schedules() -> list[DailySchedule]:
    # Imported here, not at module level: core must not import app modules at
    # import time, and attachments' tasks pull in its services and models.
    from apps.attachments.tasks import purge_attachments
    from apps.contracts.tasks import contract_expiry, contract_renewal_notice
    from apps.maintenance.tasks import maintenance_overdue, plan_maintenance
    from core.tasks import prune_task_results, purge_idempotency_keys

    return [
        # Just after midnight, so a contract that ended yesterday is closed —
        # and its successor active — before anybody records a visit today.
        DailySchedule("contract_expiry", contract_expiry, time(0, 30)),
        # After contract_expiry, so a lift whose contract ended last night is
        # cancelled out of the plan, and one whose renewal was activated stays.
        DailySchedule("plan_maintenance", plan_maintenance, time(1, 0)),
        DailySchedule("purge_idempotency_keys", purge_idempotency_keys, time(3, 0)),
        DailySchedule("purge_attachments", purge_attachments, time(3, 15)),
        DailySchedule("prune_task_results", prune_task_results, time(3, 30)),
        # Before the working day, so the e-mails are waiting when it starts.
        DailySchedule("contract_renewal_notice", contract_renewal_notice, time(6, 0)),
        DailySchedule("maintenance_overdue", maintenance_overdue, time(7, 0)),
    ]


def enqueue_due(
    now: datetime | None = None, schedules: Sequence[DailySchedule] | None = None
) -> list[str]:
    """Enqueue every job that is due and has not run today. Returns their names."""
    local_now = (now or timezone.now()).astimezone(LOCAL_TZ)
    started = []
    for schedule in get_schedules() if schedules is None else schedules:
        if not schedule.is_due(local_now):
            continue
        period = schedule.period(local_now)
        # Checked first so a job that already ran does not hit the constraint
        # every minute for the rest of the day; the constraint below is what
        # actually decides a race between two schedulers.
        if _already_ran(schedule.name, period):
            continue
        try:
            with transaction.atomic():
                run = ScheduledRun.objects.create(job=schedule.name, period=period)
                result = schedule.task.enqueue()
                run.task_id = str(result.id)
                run.save(update_fields=["task_id"])
        except IntegrityError:
            continue
        started.append(schedule.name)
    return started


def _already_ran(job: str, period: str) -> bool:
    return ScheduledRun.objects.filter(job=job, period=period).exists()
