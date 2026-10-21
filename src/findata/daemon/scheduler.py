"""Periodic jobs inside the daemon: the home for scheduled sync. No jobs are registered yet.

One background thread runs each registered job every `every_s` seconds, the first time one
interval after `start()` and then `every_s` after the previous run finished, so a slow job
never piles up. A failing job keeps its schedule and is logged with its name and exception
type only, never the message (it could hold data such as merchant names or amounts).
A scheduler is single-use: once stopped it does not start again.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    name: str
    every_s: float
    fn: Callable[[], None]
    due: float = 0.0


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, name: str, every_s: float, fn: Callable[[], None]) -> None:
        if every_s <= 0:
            raise ValueError("every_s must be positive")
        if self._thread is not None:
            raise RuntimeError("Register jobs before start().")
        self._jobs.append(_Job(name, every_s, fn))

    def start(self) -> None:
        """Start the scheduler thread; with no jobs registered there is nothing to run."""
        if self._thread is not None or not self._jobs or self._stopping.is_set():
            return
        now = time.monotonic()
        for job in self._jobs:
            job.due = now + job.every_s
        self._thread = threading.Thread(target=self._run, name="findata-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop scheduling and wait (up to `timeout`) for a job that is running to finish."""
        self._stopping.set()
        if self._thread is None:
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("A scheduled job is still running after %.0fs; not waiting.", timeout)

    def _run(self) -> None:
        while not self._stopping.is_set():
            job = min(self._jobs, key=lambda j: j.due)
            if self._stopping.wait(max(0.0, job.due - time.monotonic())):
                return
            try:
                job.fn()
            except Exception as exc:
                logger.warning("Scheduled job %r failed (%s)", job.name, type(exc).__name__)
            job.due = time.monotonic() + job.every_s
