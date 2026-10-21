"""Errors a daemon client can raise. None of them ever carries the credential."""

from __future__ import annotations

from typing import Any

NOT_RUNNING_MESSAGE = "The findata daemon is not running. Start it with `findata start`."


class DaemonClientError(Exception):
    """Base class: anything that stops a client call from completing."""


class DaemonNotRunning(DaemonClientError):
    """The daemon could not be reached (no socket, nothing listening, or no answer in time)."""


class DaemonRejected(DaemonClientError):
    """The daemon answered with a non-2xx status."""

    def __init__(
        self,
        error_code: str,
        detail: str,
        status: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{error_code} ({status}): {detail}")
        self.error_code = error_code
        self.detail = detail
        self.status = status
        self.extra = extra or {}
