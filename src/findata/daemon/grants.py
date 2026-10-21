"""In-memory write grants and single-use approvals. Never persisted: a restart clears them."""

import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from findata.wire import GrantOut

GRANTABLE = frozenset({"sync", "link", "items.remove"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GrantStore:
    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        self._lock = threading.Lock()
        self._grant: GrantOut | None = None

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
            self._grant = out
        return out

    def revoke(self) -> None:
        with self._lock:
            self._grant = None

    def current(self) -> GrantOut | None:
        with self._lock:
            if self._grant is not None and self._clock() >= self._grant.expires_at:
                self._grant = None
            return self._grant

    def allows(self, operation: str) -> bool:
        current = self.current()
        return current is not None and operation in current.operations


class ApprovalStore:
    def __init__(self, clock: Callable[[], datetime] = _utcnow, ttl_s: int = 300):
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_s)
        self._lock = threading.Lock()
        self._requests: dict[str, dict] = {}

    def _purge(self) -> None:
        now = self._clock()
        for rid in [r for r, v in self._requests.items() if now >= v["created_at"] + self._ttl]:
            del self._requests[rid]

    def request(self, operation: str, subject: str) -> str:
        rid = secrets.token_urlsafe(8)
        with self._lock:
            self._purge()
            self._requests[rid] = {
                "operation": operation,
                "subject": subject,
                "created_at": self._clock(),
                "approved": False,
            }
        return rid

    def approve(self, request_id: str) -> None:
        with self._lock:
            self._purge()
            self._requests[request_id]["approved"] = True  # KeyError if unknown/expired

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
            return True

    def pending(self) -> list[dict]:
        with self._lock:
            self._purge()
            return [
                {
                    "request_id": rid,
                    "operation": v["operation"],
                    "subject": v["subject"],
                    "created_at": v["created_at"],
                }
                for rid, v in self._requests.items()
                if not v["approved"]
            ]
