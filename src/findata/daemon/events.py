"""Daemon events pushed to streaming clients, so findata's processes never poll each other.

`EventBus.publish` may be called from any thread (route handlers run in the thread pool, jobs
and timers on threads of their own). It records the event with the next `seq` in a bounded
buffer and wakes the open streams through each one's event loop (`loop.call_soon_threadsafe`).
A stream is a coroutine waiting to be woken: while idle it holds no thread, and nothing in it
sleeps in a loop to check for news.

Events carry only ids, institution names, counts, statuses, operations and expiry times, never
transaction data or secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic_core import to_json

from findata.wire import GAP_EVENT, KEEPALIVE_EVENT

CAPACITY = 256  # events kept for streams that resume with `after`
MAX_STREAMS = 32  # streams open at once, daemon-wide and job streams together
# Of those, the agent (mcp) may hold this many, so the humans (cli, web) always have the rest:
# an agent needs only `read` to open streams, and an idle stream never times out.
MAX_AGENT_STREAMS = 8
KEEPALIVE_S = 15.0  # a stream silent this long sends a keep-alive line


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def ndjson(obj: Any) -> bytes:
    """`obj` as one NDJSON line, with datetimes written as the JSON API writes them."""
    return to_json(obj) + b"\n"


GAP_LINE = ndjson({"event": GAP_EVENT})
KEEPALIVE_LINE = ndjson({"event": KEEPALIVE_EVENT})


class TooManyStreams(Exception):
    """Every stream slot is taken (`agent` False), or every slot the agent may hold (True);
    `limit` is the cap that was reached."""

    def __init__(self, limit: int, *, agent: bool) -> None:
        super().__init__(limit)
        self.limit = limit
        self.agent = agent


class Stream:
    """One open stream's slot on the bus and its wake-up: `wake()` from any thread, `wait()`
    in the event loop the stream runs in."""

    def __init__(self, loop: asyncio.AbstractEventLoop, opened_after: int) -> None:
        self._loop = loop
        self._woken = asyncio.Event()
        self.opened_after = opened_after  # the bus's last seq when the stream opened

    def wake(self) -> None:
        # A RuntimeError means its event loop has closed, so the stream has ended anyway.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._woken.set)

    async def wait(self, timeout: float) -> bool:
        """True once woken (since the last wait), False after `timeout` seconds without.

        The caller reads what is new right after this returns, before it awaits anything
        else, so a wake-up that comes in between is never lost: it just wakes the next wait.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._woken.wait()
        except TimeoutError:
            return False
        self._woken.clear()
        return True


class EventBus:
    def __init__(
        self,
        capacity: int = CAPACITY,
        max_subscribers: int = MAX_STREAMS,
        *,
        max_agent_streams: int = MAX_AGENT_STREAMS,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        if capacity < 1 or max_subscribers < 1 or max_agent_streams < 0:
            raise ValueError("stream and buffer limits must be positive")
        self._clock = clock
        self._max = max_subscribers
        self._max_agent = max_agent_streams
        self._lock = threading.Lock()
        self._seq = 0
        self._buffer: deque[tuple[int, bytes]] = deque(maxlen=capacity)  # (seq, line)
        self._streams: set[Stream] = set()  # every open stream; all are woken on close
        self._followers: set[Stream] = set()  # streams of bus events, woken by each publish
        self._agent: set[Stream] = set()  # the open streams the agent (mcp) holds
        self._closed = False

    def publish(self, event: str, **fields: Any) -> int:
        """Record `event` as `{"seq", "event", "at", **fields}` and wake the streams following
        the bus; returns its seq. The line is written once here, whoever reads it later."""
        with self._lock:
            seq = self._seq + 1
            line = ndjson({"seq": seq, "event": event, "at": self._clock(), **fields})
            self._seq = seq
            self._buffer.append((seq, line))
            followers = list(self._followers)
        for stream in followers:
            stream.wake()
        return seq

    def read(self, after: int) -> tuple[bool, bytes, int]:
        """`(gap, lines, last)`: the buffered events with seq > `after` as NDJSON lines, and
        the seq to read after next time. `gap` means some events after `after` are gone (the
        buffer moved on, or `after` was never issued by this bus, e.g. it came from before a
        restart); the reader should re-read the state it tracks."""
        with self._lock:
            if after > self._seq:
                return True, b"", self._seq
            first = self._buffer[0][0] if self._buffer else self._seq + 1
            lines = b"".join(line for seq, line in self._buffer if seq > after)
            return after + 1 < first, lines, self._seq

    def open_stream(self, *, follow: bool, agent: bool = False) -> Stream:
        """Take a stream slot for a coroutine in the running event loop, or TooManyStreams
        once `max_subscribers` are open, or, for the `agent`, once it holds
        `max_agent_streams`.

        A `follow` stream is woken by every publish; every stream is woken by `close`. The
        caller gives the slot back with `close_stream` however the stream ends.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if len(self._streams) >= self._max:
                raise TooManyStreams(self._max, agent=False)
            if agent and len(self._agent) >= self._max_agent:
                raise TooManyStreams(self._max_agent, agent=True)
            stream = Stream(loop, self._seq)
            self._streams.add(stream)
            if follow:
                self._followers.add(stream)
            if agent:
                self._agent.add(stream)
        return stream

    def close_stream(self, stream: Stream) -> None:
        with self._lock:
            self._streams.discard(stream)
            self._followers.discard(stream)
            self._agent.discard(stream)

    def close(self) -> None:
        """The daemon is stopping: wake every open stream so it ends (after sending what it
        has), and make streams opened from now on end at once."""
        with self._lock:
            self._closed = True
            streams = list(self._streams)
        for stream in streams:
            stream.wake()

    @property
    def closed(self) -> bool:
        return self._closed
