"""The scheduler: which jobs run, when, and that each runs once a day.

See docs/superpowers/specs/2026-10-20-background-jobs-design.md. The suite uses
the ImmediateBackend, so an enqueued task has already run by the time
`enqueue_due` returns — which is what lets these tests check the jobs' effects
and not just that something was queued.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.management import call_command
from django.tasks import task
from django.test import override_settings
from django.utils import timezone

from apps.users.services import register_company
from core import scheduling
from core.models import IdempotencyKey, ScheduledRun
from core.scheduling import DailySchedule, enqueue_due
from core.tasks import prune_task_results

calls: list[str] = []


@task
def record_call() -> None:
    calls.append("ran")


@task
def explode() -> None:
    raise RuntimeError("the nightly job broke")


@pytest.fixture(autouse=True)
def reset_calls():
    calls.clear()


def at_istanbul(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant, written as Istanbul wall-clock time (UTC+3, no DST)."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=3)


NIGHTLY = DailySchedule(name="nightly", task=record_call, at=time(3, 0))
# After every real job's time, so all of them are due.
LATE_EVENING = at_istanbul(2026, 10, 20, 23, 0)


@pytest.mark.django_db
class TestWhenAJobIsDue:
    def test_not_before_its_time(self):
        assert enqueue_due(at_istanbul(2026, 10, 20, 2, 59), [NIGHTLY]) == []
        assert calls == []

    def test_at_its_time(self):
        assert enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY]) == ["nightly"]
        assert calls == ["ran"]

    def test_late_the_same_day_when_the_scheduler_was_down_at_three(self):
        assert enqueue_due(at_istanbul(2026, 10, 20, 17, 45), [NIGHTLY]) == ["nightly"]

    def test_the_istanbul_date_decides_the_day_not_the_utc_one(self):
        # 22:30 UTC on the 20th is 01:30 on the 21st in Istanbul: the 21st's run
        # is not due yet, and the 20th's is a different day entirely.
        enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY])
        late_evening_utc = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)

        assert enqueue_due(late_evening_utc, [NIGHTLY]) == []
        assert list(ScheduledRun.objects.values_list("period", flat=True)) == ["2026-10-20"]


@pytest.mark.django_db
class TestOncePerDay:
    def test_a_second_tick_the_same_day_enqueues_nothing(self):
        enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY])
        enqueue_due(at_istanbul(2026, 10, 20, 3, 1), [NIGHTLY])

        assert calls == ["ran"]
        assert ScheduledRun.objects.count() == 1

    def test_the_next_day_runs_again(self):
        enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY])
        enqueue_due(at_istanbul(2026, 10, 21, 3, 0), [NIGHTLY])

        assert calls == ["ran", "ran"]

    def test_a_run_recorded_by_another_scheduler_wins_the_race(self):
        # Two schedulers can both see "no run yet" before either inserts. The
        # unique constraint is what decides, not the check before it.
        ScheduledRun.objects.create(job="nightly", period="2026-10-20")
        with mock.patch.object(scheduling, "_already_ran", return_value=False):
            assert enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY]) == []
        assert calls == []

    def test_an_enqueue_that_fails_leaves_no_run_behind_so_the_next_tick_retries(self):
        failing_task = mock.Mock()
        failing_task.enqueue.side_effect = ConnectionError("database went away")
        broken = DailySchedule(name="nightly", task=failing_task, at=time(3, 0))

        with pytest.raises(ConnectionError):
            enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [broken])
        assert ScheduledRun.objects.count() == 0

        assert enqueue_due(at_istanbul(2026, 10, 20, 3, 1), [NIGHTLY]) == ["nightly"]

    def test_the_run_records_which_task_it_started(self):
        enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [NIGHTLY])
        assert ScheduledRun.objects.get().task_id


@pytest.mark.django_db
class TestAFailedJobIsLoud:
    def test_it_is_logged_at_error_with_its_traceback(self, caplog: Any):
        boom = DailySchedule(name="boom", task=explode, at=time(3, 0))
        with caplog.at_level(logging.INFO, logger="django.tasks"):
            enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [boom])

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "explode" in errors[0].getMessage()
        assert errors[0].exc_info is not None

    def test_it_still_counts_as_todays_run(self):
        # A job that raises every time must not be retried every minute; it is
        # reported once and tried again tomorrow.
        boom = DailySchedule(name="boom", task=explode, at=time(3, 0))
        enqueue_due(at_istanbul(2026, 10, 20, 3, 0), [boom])
        assert enqueue_due(at_istanbul(2026, 10, 20, 3, 1), [boom]) == []


class TestTheSchedule:
    def test_names_are_unique(self):
        names = [s.name for s in scheduling.get_schedules()]
        assert len(names) == len(set(names))

    def test_the_jobs_that_were_waiting_for_a_scheduler_are_on_it(self):
        assert {s.name for s in scheduling.get_schedules()} >= {
            "purge_attachments",
            "purge_idempotency_keys",
            "prune_task_results",
        }


@pytest.mark.django_db
class TestTheScheduledJobsDoTheirWork:
    def test_expired_idempotency_keys_are_purged(self):
        company, owner = register_company(
            legal_name="Firm Ltd",
            display_name="Firm",
            first_name="F",
            last_name="Owner",
            email="owner@example.com",
            password="correct-horse-battery",
        )
        now = timezone.now()
        for key, expires in (("old", now - timedelta(days=1)), ("live", now + timedelta(days=1))):
            IdempotencyKey.objects.create(
                company=company,
                user=owner,
                key=key,
                endpoint="/x",
                request_hash="h",
                response_status=201,
                response_body={},
                expires_at=expires,
            )

        enqueue_due(LATE_EVENING, _only("purge_idempotency_keys"))

        assert list(IdempotencyKey.objects.values_list("key", flat=True)) == ["live"]

    def test_the_attachment_purge_runs_across_every_company(self):
        with mock.patch("apps.attachments.tasks.purge_detached_objects", return_value=3) as purge:
            enqueue_due(LATE_EVENING, _only("purge_attachments"))
        purge.assert_called_once_with()

    def test_the_result_prune_is_a_no_op_without_the_database_backend(self, caplog: Any):
        # The suite runs on the ImmediateBackend, which keeps no results to prune.
        with caplog.at_level(logging.ERROR, logger="django.tasks"):
            enqueue_due(LATE_EVENING, _only("prune_task_results"))
        assert caplog.records == []

    def test_the_result_prune_runs_on_the_database_backend(self):
        database = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}
        with override_settings(TASKS=database):
            prune_task_results.call()


def _only(name: str) -> list[DailySchedule]:
    return [s for s in scheduling.get_schedules() if s.name == name]


@pytest.fixture
def keep_the_test_connection():
    # Each tick calls close_old_connections(), which closes a connection whose
    # autocommit differs from its setting — true of every connection inside a
    # test's transaction, so on PostgreSQL the tick would close the test's own
    # connection. Django's test client disconnects the same handler for the
    # same reason.
    with mock.patch("core.management.commands.run_scheduler.close_old_connections"):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("keep_the_test_connection")
class TestTheCommand:
    def test_once_runs_a_single_tick_and_returns(self, capsys: Any):
        with mock.patch.object(scheduling, "get_schedules", return_value=[NIGHTLY]):
            with mock.patch.object(timezone, "now", return_value=at_istanbul(2026, 10, 20, 3, 0)):
                call_command("run_scheduler", "--once")

        assert calls == ["ran"]
        assert "nightly" in capsys.readouterr().out

    def test_a_tick_that_fails_does_not_stop_the_loop(self):
        ticks: list[Any] = [ConnectionError("database blip"), ["nightly"]]

        def tick(*_: Any, **__: Any) -> list[str]:
            result = ticks.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        stop = mock.Mock()
        stop.wait.side_effect = [False, True]
        with (
            mock.patch("core.management.commands.run_scheduler.enqueue_due", side_effect=tick),
            mock.patch(
                "core.management.commands.run_scheduler.Command.create_stop_event",
                return_value=stop,
            ),
        ):
            call_command("run_scheduler", "--interval", "1")

        assert ticks == []
        stop.wait.assert_called_with(1)
