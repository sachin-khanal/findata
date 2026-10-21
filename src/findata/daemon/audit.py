"""The audit log: one row per daemon request, written by a single ASGI middleware.

What is recorded is deliberately narrow: the caller's role, the route's operation name, a
summary made only of the HTTP method, the route template and the *names* of the query
parameters the route declares, and the outcome. Never query values, request or response
bodies, credentials, or transaction data.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from findata.db import AuditEntry

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"  # the role recorded when no valid credential was presented
UNKNOWN_OPERATION = "unknown"  # no route matched the path and method
SCOPE_KEY = "findata.audit"  # where the middleware leaves `RequestAudit` in the ASGI scope


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class AuditLog:
    """Writes and reads the `audit_log` table."""

    def __init__(
        self, session_factory: sessionmaker, clock: Callable[[], dt.datetime] = _utcnow
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def record(self, role: str, operation: str, summary: str, outcome: str) -> None:
        entry = AuditEntry(
            at=self._clock().astimezone(dt.UTC),
            role=role[:16],
            operation=operation[:64],
            summary=summary[:255],
            outcome=outcome[:32],
        )
        with self._session_factory() as session:
            session.add(entry)
            session.commit()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest `limit` entries, newest first."""
        with self._session_factory() as session:
            rows = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit))
            return [
                {
                    "id": e.id,
                    # SQLite drops the zone; entries are always written in UTC.
                    "at": e.at if e.at.tzinfo else e.at.replace(tzinfo=dt.UTC),
                    "role": e.role,
                    "operation": e.operation,
                    "summary": e.summary,
                    "outcome": e.outcome,
                }
                for e in rows
            ]


@dataclass
class RequestAudit:
    """Facts about one request that later stages fill in; the auth dependency sets `role`."""

    role: str = ANONYMOUS


def outcome_for(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "denied"
    return "error"


def _declared_query_names(route: Any) -> set[str]:
    """Query parameter names the route (or its dependencies) declares."""
    names: set[str] = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        dependant = stack.pop()
        if dependant is not None:
            names.update(param.alias for param in dependant.query_params)
            stack.extend(dependant.dependencies)
    return names


def summarize(scope: Scope) -> tuple[str, str]:
    """(operation, summary) for a request, built only from the matched route.

    The raw path is never used (it can hold ids), and only query parameter names the route
    declares are listed; any others are just counted, since a name can carry data too.
    """
    method = scope["method"]
    route = scope.get("route")
    template = getattr(route, "path", None)
    if template is None:
        return UNKNOWN_OPERATION, f"{method} (no matching route)"
    methods = getattr(route, "methods", None)
    operation = route.name if not methods or method in methods else UNKNOWN_OPERATION
    sent = set(QueryParams(scope.get("query_string", b"")).keys())
    declared = _declared_query_names(route)
    summary = f"{method} {template}"
    if known := sorted(sent & declared):
        summary += f" query=[{','.join(known)}]"
    if undeclared := len(sent - declared):
        summary += f" +{undeclared} undeclared"
    return operation, summary


class AuditMiddleware:
    """The one place every HTTP request is audited, after it finishes (401s and errors too)."""

    def __init__(self, app: ASGIApp, audit: AuditLog) -> None:
        self.app = app
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        facts = RequestAudit()
        scope[SCOPE_KEY] = facts
        status = 500  # stays 500 if the app raises before it responds

        async def send_and_note_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            with anyio.CancelScope(shield=True):  # a dropped client still gets audited
                await self._record(scope, facts.role, status)

    async def _record(self, scope: Scope, role: str, status: int) -> None:
        operation = UNKNOWN_OPERATION
        try:
            operation, summary = summarize(scope)
            await run_in_threadpool(
                self.audit.record, role, operation, summary, outcome_for(status)
            )
        except Exception as exc:  # auditing must never fail the request
            # Only the operation name and error type: the message could echo SQL parameters.
            logger.warning(
                "Could not write the audit entry for %s (%s)", operation, type(exc).__name__
            )
