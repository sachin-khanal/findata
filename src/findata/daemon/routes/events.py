"""Event streams: the daemon's events and one job's progress, as NDJSON pushed as they happen.

Each stream is an async generator that waits on its wake-up (`findata.daemon.events.Stream`),
so an idle stream holds no thread, no database session and no pooled connection. After
`keepalive_s` without anything to send it writes a keep-alive line, which is also how a
client that went away is noticed. A stream ends when its client disconnects or the daemon
stops, and gives its slot back however it ends.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

from fastapi import APIRouter, Query
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from findata.daemon.auth import Role
from findata.daemon.deps import DaemonError, DaemonState, RoleDep, StateDep, require
from findata.daemon.events import (
    GAP_LINE,
    KEEPALIVE_LINE,
    Stream,
    TooManyStreams,
    ndjson,
)
from findata.wire import JOB_FINISHED_EVENT, NDJSON

router = APIRouter(prefix="/v1", dependencies=[require("read")])


class NdjsonStream(StreamingResponse):
    """A streamed NDJSON response that calls `on_close` once it ends, however it ends."""

    media_type = NDJSON

    def __init__(self, lines: AsyncGenerator[bytes], on_close: Callable[[], None]) -> None:
        super().__init__(lines)
        self._lines = lines
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()
            await self._lines.aclose()  # ends a generator left waiting at a `yield`


def _open(state: DaemonState, role: Role, *, follow: bool) -> Stream:
    try:
        return state.events.open_stream(follow=follow, agent=role == "mcp")
    except TooManyStreams as exc:
        if exc.agent:
            detail = (
                f"The agent (mcp) role already has {exc.limit} event streams open, the most it "
                "may hold at once; close one and try again."
            )
        else:
            detail = (
                f"The daemon already serves {exc.limit} event streams, the most it allows at "
                "once; close one and try again."
            )
        raise DaemonError(503, "TOO_MANY_STREAMS", detail) from None


async def _daemon_lines(state: DaemonState, stream: Stream, after: int) -> AsyncGenerator[bytes]:
    bus = state.events
    while True:
        gap, lines, after = bus.read(after)
        if gap or lines:
            yield (GAP_LINE if gap else b"") + lines
        if bus.closed:
            return
        if not await stream.wait(state.keepalive_s):
            yield KEEPALIVE_LINE


@router.get("/events", name="events")
async def follow_events(
    state: StateDep,
    role: RoleDep,
    after: int | None = Query(None, ge=0, description="resume after this seq"),
) -> NdjsonStream:
    """Daemon events as `{"seq", "event", "at", ...}` lines. Without `after`, only those
    published from now on; with it, first the buffered ones after that seq (a `gap` line
    first if some of them are gone)."""
    stream = _open(state, role, follow=True)
    start = stream.opened_after if after is None else after
    return NdjsonStream(
        _daemon_lines(state, stream, start), lambda: state.events.close_stream(stream)
    )


async def _job_lines(state: DaemonState, job_id: str, stream: Stream) -> AsyncGenerator[bytes]:
    sent = 0
    while True:
        job = state.jobs.get(job_id)  # never evicted while watched
        lines = b"".join(ndjson(e) for e in job.events[sent:])
        sent = len(job.events)
        if job.state != "running":
            finished = {"event": JOB_FINISHED_EVENT, "state": job.state, "result": job.result}
            yield lines + ndjson(finished)
            return
        if lines:
            yield lines
        if state.events.closed:
            return
        if not await stream.wait(state.keepalive_s):
            yield KEEPALIVE_LINE


@router.get("/jobs/{job_id}/events", name="jobs.events")
async def follow_job(job_id: str, state: StateDep, role: RoleDep) -> NdjsonStream:
    """The job's events so far, then each new one as it happens (the same dicts as
    `GET /v1/jobs/{id}`), then `job_finished`; then the stream ends."""
    stream = _open(state, role, follow=False)
    try:
        state.jobs.watch(job_id, stream.wake)
    except KeyError:
        state.events.close_stream(stream)
        raise DaemonError(404, "NOT_FOUND", "No such job.") from None

    def close() -> None:
        state.jobs.unwatch(job_id, stream.wake)
        state.events.close_stream(stream)

    return NdjsonStream(_job_lines(state, job_id, stream), close)
