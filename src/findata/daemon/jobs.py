"""Background jobs: run a function on a worker thread and let clients poll its events.

A job's events are served to clients, so a failure that is not a `PlaidError` is reduced to a
generic event: the exception's message (which may hold data) is never recorded, only its type
is logged.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable

from findata.plaid_gateway import PlaidError
from findata.wire import JobOut

logger = logging.getLogger(__name__)

MAX_JOBS = 50
GENERIC_ERROR = "Unexpected error in the daemon; see its log."

JobFn = Callable[[Callable[[dict], None]], dict]


class _Job:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.state = "running"
        self.events: list[dict] = []
        self.result: dict | None = None


class JobManager:
    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._max = max_jobs
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}  # insertion order = age

    def start(self, fn: JobFn) -> str:
        job = _Job(secrets.token_urlsafe(8))
        with self._lock:
            self._jobs[job.id] = job
            self._evict()
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job.id

    def get(self, job_id: str) -> JobOut:
        """The job's state and a copy of its events so far; KeyError if unknown."""
        with self._lock:
            job = self._jobs[job_id]
            return JobOut(
                id=job.id, state=job.state, events=[dict(e) for e in job.events], result=job.result
            )

    def _evict(self) -> None:
        for job_id in list(self._jobs):
            if len(self._jobs) <= self._max:
                return
            if self._jobs[job_id].state != "running":
                del self._jobs[job_id]

    def _emit(self, job: _Job, event: dict) -> None:
        with self._lock:
            job.events.append(dict(event))

    def _run(self, job: _Job, fn: JobFn) -> None:
        try:
            result = fn(lambda event: self._emit(job, event))
        except PlaidError as exc:
            self._finish(
                job,
                "failed",
                None,
                {
                    "event": "error",
                    "message": f"Plaid rejected the request ({exc.error_code}): {exc}",
                    "error_code": exc.error_code,
                },
            )
        except Exception as exc:
            logger.error("Job %s failed with %s", job.id, type(exc).__name__)
            self._finish(
                job,
                "failed",
                None,
                {"event": "error", "message": GENERIC_ERROR, "error_code": "INTERNAL_ERROR"},
            )
        else:
            self._finish(job, "done", result, None)

    def _finish(self, job: _Job, state: str, result: dict | None, event: dict | None) -> None:
        with self._lock:
            if event is not None:
                job.events.append(event)
            job.result = result
            job.state = state
            self._evict()
