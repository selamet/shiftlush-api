# Background jobs and a clock

Date: 2026-10-20. Roadmap: M0 item 3 (`2026-10-20-phase-2-roadmap.md`, #85).

## Why

Every Phase 2 module after M1 runs something on a schedule or out of band:
- contract renewal and expiry (M2)
- notification e-mail (M2)
- the monthly maintenance plan (M3)
- inspection reminders (M5)
- billing drafts (M7)

Two jobs already exist and have never run. `purge_attachments` and
`purge_idempotency_keys` both say *"Scheduling belongs to the deployment…
scheduling is the only thing left to decide."* So production keeps every
deleted attachment's bytes forever, and the idempotency table grows without
bound.

This decides the scheduling, and ships those two jobs on it as the proof.

## Decisions

- **`django.tasks` with the `django-tasks-db` `DatabaseBackend`.**
  - Spec §2.5 says background work uses `django.tasks`, not Celery.
  - Django 6.1 ships only the `immediate` and `dummy` backends, so a worker
    needs a third-party backend. `django-tasks-db` 0.13 is the reference ORM
    backend, and on Django ≥ 6 it plugs into `django.tasks` directly. A local
    check enqueued a task, ran `db_worker --batch`, and got `SUCCESSFUL`.
  - The queue is the PostgreSQL database the app already uses. A task
    enqueued inside a transaction is committed or rolled back with the data
    it describes.
- **`REDIS_QUEUE_URL` stays unread.**
  - The non-evicting Redis instance in `DEPLOYMENT.md` was provisioned for a
    broker that is not needed at this volume: tens of jobs a day, and e-mail
    in the hundreds.
  - Revisit if task throughput ever makes the `db_worker` poll a measurable
    load on the database.
- **Schedules live in code, not in the deployment.**
  - `core/scheduling.py` holds a list of `DailySchedule(name, task, at)`.
  - `at` is wall-clock time in `Europe/Istanbul`, the only market the
    product is sold in. `TIME_ZONE` stays `UTC`.
- **Idempotent by construction.**
  - Each run inserts `ScheduledRun(job, period)` under a unique constraint,
    where `period` is the local date, and enqueues the task in the same
    transaction.
  - So a second scheduler, a restart, or two ticks in the same minute
    enqueue nothing new.
  - If the enqueue fails, the row rolls back with it and the next tick tries
    again.
- **Late runs, but no backfill.**
  - A job is due once `now ≥ at` on a local date that has no run yet. A
    scheduler that was down at 03:00 runs the job when it comes back the
    same day.
  - A whole missed day is not replayed. Every job here works off "as of
    now", so running it twice would not do more.
- **Two new containers, from the same image:**
  - `worker` runs `db_worker`.
  - `scheduler` runs `run_scheduler`, a loop that ticks every 60 s.
  - Neither runs the entrypoint's migrations. Both wait for `api` to report
    healthy, which is after migrations have been applied.
- **Locally, nothing to start.**
  - `development` and `test` use the `ImmediateBackend`: a task runs inline
    when it is enqueued.
  - `run_scheduler --once` runs a single tick by hand.
- **Failures are loud.**
  - A task that raises is recorded `FAILED` in its result row, and logged at
    `ERROR` with its name and id.
  - Sentry's logging integration turns that log into an event, so a broken
    nightly job is not silent.
- **Results are pruned.**
  - A third daily job runs `prune_db_task_results`.
  - Successful results are kept 7 days, failed results 30 days.

## Out of scope

- Any job other than the two purges and the prune. M2 adds the first
  business jobs.
- Schedules other than daily. M3's monthly plan adds a `period` function for
  months when it needs one.
- An admin screen for task results.

## Acceptance criteria

- [ ] `TASKS` configured per environment; `django_tasks_db` installed and migrated
- [ ] `ScheduledRun` with a unique (`job`, `period`)
- [ ] `run_scheduler` (`--once`, `--interval`) enqueues each due job exactly
      once per local day, and exits cleanly on SIGTERM
- [ ] The two purges and the result prune are scheduled tasks
- [ ] A failed task is logged at `ERROR`
- [ ] `worker` and `scheduler` services in `docker-compose.prod.yml`, with
      memory limits
- [ ] `DEPLOYMENT.md` says what runs, when, and how to check it
- [ ] `make lint && make mypy && make check && make test` pass
