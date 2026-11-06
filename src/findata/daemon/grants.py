"""In-memory write grants and single-use approvals. Never persisted: a restart clears them.

With an event bus, each store publishes its own changes (`grant_*`, `approval_*`), and an
expiry is published by a timer armed for the expiry instant. The lazy checks below still
decide what is allowed; they never publish, so every grant and request ends with exactly one
event, from whichever of revoke, use or timer comes first.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from findata.daemon.events import EventBus
from findata.wire import GrantOut

GRANTABLE = frozenset({"sync", "link", "items.remove"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Timer(Protocol):
    def cancel(self) -> None: ...


# Starts a timer that calls `fn` once after `delay_s` seconds unless cancelled first.
StartTimer = Callable[[float, Callable[[], None]], Timer]


def start_timer(delay_s: float, fn: Callable[[], None]) -> Timer:
    """A thread that waits for the deadline (not a loop) and then calls `fn`."""
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()
    return timer


class GrantStore:
    def __init__(
        self,
        clock: Callable[[], datetime] = _utcnow,
        *,
        events: EventBus | None = None,
        timer: StartTimer = start_timer,
    ):
        self._clock = clock
        self._events = events
        self._timer = timer
        self._lock = threading.Lock()
        self._grant: GrantOut | None = None
        self._expiry: tuple[GrantOut, Timer] | None = None  # the grant whose timer is armed

    def grant(self, operations: set[str], ttl_s: int) -> GrantOut:
        if not operations or not operations <= GRANTABLE:
            raise ValueError(f"operations must be a non-empty subset of {sorted(GRANTABLE)}")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        out = GrantOut(
            operations=sorted(operations),
            expires_at=self._clock() + timedelta(seconds=ttl_s),
        )
        with self._lock:
            self._disarm()  # a replaced grant never reports an expiry
            self._grant = out
            if self._events is not None:
                self._expiry = (out, self._timer(ttl_s, lambda: self._expire(out)))
                self._publish("grant_created", out)
        return out

    def revoke(self) -> None:
        with self._lock:
            live = self._live()
            self._grant = None
            if live is not None:  # an expired grant's timer is left to report its expiry
                self._disarm()
                self._publish("grant_revoked", live)

    def current(self) -> GrantOut | None:
        with self._lock:
            return self._live()

    def allows(self, operation: str) -> bool:
        current = self.current()
        return current is not None and operation in current.operations

    def _live(self) -> GrantOut | None:
        if self._grant is not None and self._clock() >= self._grant.expires_at:
            self._grant = None
        return self._grant

    def _disarm(self) -> None:
        if self._expiry is not None:
            self._expiry[1].cancel()
            self._expiry = None

    def _expire(self, grant: GrantOut) -> None:
        with self._lock:
            if self._expiry is None or self._expiry[0] is not grant:
                return  # revoked or replaced first (its timer was already running)
            self._expiry = None
            if self._grant is grant:
                self._grant = None
            self._publish("grant_expired", grant)

    def _publish(self, event: str, grant: GrantOut) -> None:
        if self._events is not None:
            self._events.publish(event, operations=grant.operations, expires_at=grant.expires_at)


class ApprovalStore:
    def __init__(
        self,
        clock: Callable[[], datetime] = _utcnow,
        ttl_s: int = 300,
        *,
        events: EventBus | None = None,
        timer: StartTimer = start_timer,
    ):
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_s)
        self._events = events
        self._timer = timer
        self._lock = threading.Lock()
        self._requests: dict[str, dict] = {}
        self._expiry: dict[str, Timer] = {}  # request id -> its armed expiry timer

    def _purge(self) -> None:
        now = self._clock()
        for rid in [r for r, v in self._requests.items() if now >= v["created_at"] + self._ttl]:
            del self._requests[rid]

    def request(self, operation: str, subject: str, *, institution: str | None = None) -> str:
        """A pending request for a human to approve; `institution` only names it in events."""
        # 64 random bits as 16 hex characters: never starts with "-", so `findata approve` takes it
        rid = secrets.token_hex(8)
        with self._lock:
            self._purge()
            req = {
                "operation": operation,
                "subject": subject,
                "institution": institution,
                "created_at": self._clock(),
                "approved": False,
            }
            self._requests[rid] = req
            if self._events is not None:
                ttl_s = self._ttl.total_seconds()
                self._expiry[rid] = self._timer(ttl_s, lambda: self._expire(rid, req))
                self._publish("approval_requested", rid, req)
        return rid

    def approve(self, request_id: str) -> None:
        with self._lock:
            self._purge()
            req = self._requests[request_id]  # KeyError if unknown/expired
            if not req["approved"]:
                req["approved"] = True
                self._publish("approval_approved", request_id, req)

    def consume(self, request_id: str, operation: str, subject: str) -> bool:
        with self._lock:
            self._purge()
            req = self._requests.get(request_id)
            if (
                req is None
                or not req["approved"]
                or req["operation"] != operation
                or req["subject"] != subject
            ):
                return False
            del self._requests[request_id]
            timer = self._expiry.pop(request_id, None)
            if timer is not None:  # used up: it never expires
                timer.cancel()
            return True

    def pending(self) -> list[dict]:
        with self._lock:
            self._purge()
            return [
                {
                    "request_id": rid,
                    "operation": v["operation"],
                    "subject": v["subject"],
                    "institution": v["institution"],
                    "created_at": v["created_at"],
                }
                for rid, v in self._requests.items()
                if not v["approved"]
            ]

    def _expire(self, request_id: str, req: dict) -> None:
        with self._lock:
            if self._expiry.pop(request_id, None) is None:
                return  # used up first (its timer was already running)
            self._requests.pop(request_id, None)  # unless a lazy purge got there first
            self._publish("approval_expired", request_id, req)

    def _publish(self, event: str, request_id: str, req: dict) -> None:
        if self._events is not None:
            self._events.publish(
                event,
                request_id=request_id,
                operation=req["operation"],
                item_id=req["subject"],  # every request so far is about an Item
                institution=req["institution"],
            )
