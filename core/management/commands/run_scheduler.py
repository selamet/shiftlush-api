"""Enqueue scheduled jobs as they fall due. Runs in its own container.

A loop rather than a crontab entry, so the schedule ships with the code that it
schedules (see core/scheduling.py). It only enqueues; the `worker` container
runs what it enqueues.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.scheduling import enqueue_due

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enqueue scheduled background jobs as they fall due."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single tick and exit — for running a due job by hand.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Seconds between ticks. Schedules are minute-grained, so 60.",
        )

    def create_stop_event(self) -> threading.Event:
        return threading.Event()

    def handle(self, *args: Any, **options: Any) -> None:
        stop = self.create_stop_event()
        if not options["once"]:
            # `docker stop` sends SIGTERM: finish the tick in hand, then exit,
            # rather than being killed halfway through an enqueue.
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            self.stdout.write(f"scheduler started, ticking every {options['interval']}s")

        while True:
            self._tick()
            if options["once"] or stop.wait(options["interval"]):
                break

    def _tick(self) -> None:
        # A process that lives for weeks holds one connection for weeks; this
        # drops it once it is past CONN_MAX_AGE or broken, as a request would.
        close_old_connections()
        try:
            started = enqueue_due()
        except Exception:
            # A database blip must not end the loop. Logged at ERROR, which is
            # what reaches Sentry; the next tick tries again.
            logger.exception("scheduler tick failed")
            return
        for name in started:
            self.stdout.write(f"enqueued {name}")
