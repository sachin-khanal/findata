"""The daemon's internal /v1 API (FastAPI).

Every route needs a role credential (`X-Findata-Credential`) and a permission for that role;
every request, refused ones included, is written to the audit log by one middleware; every
error is `{"error_code", "detail"}`. No interactive docs: nothing is served without a credential.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from findata.daemon.audit import ANONYMOUS, SCOPE_KEY, AuditLog, AuditMiddleware
from findata.daemon.auth import CredentialStore
from findata.daemon.deps import DaemonError, DaemonState, resolve_role
from findata.daemon.grants import ApprovalStore, GrantStore
from findata.daemon.presence import PresenceChecker
from findata.daemon.routes import ROUTERS
from findata.plaid_gateway import PlaidError
from findata.runtime import Runtime

_HTTP_ERROR_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def _error(status: int, error_code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error_code": error_code, "detail": detail, **extra}
    )


def _render(exc: DaemonError) -> JSONResponse:
    return _error(exc.status, exc.error_code, exc.detail, **exc.extra)


async def _refuse_unauthenticated(request: Request) -> JSONResponse | None:
    """A 401 (or 500) for a caller the auth dependency never saw, e.g. on an unknown route or
    an unparsable body, so nothing but 401 is ever revealed without a credential."""
    facts = request.scope.get(SCOPE_KEY)
    if facts is not None and facts.role != ANONYMOUS:
        return None  # already authenticated by the route's dependency
    try:
        await run_in_threadpool(resolve_role, request)
    except DaemonError as exc:
        return _render(exc)
    return None


def _describe(errors) -> str:
    """Where and why validation failed, without echoing the rejected input."""
    parts = [
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}" for e in errors
    ]
    return "; ".join(parts) or "Invalid request."


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DaemonError)
    async def _daemon_error(_: Request, exc: DaemonError) -> JSONResponse:
        return _render(exc)

    @app.exception_handler(PlaidError)
    async def _plaid_error(_: Request, exc: PlaidError) -> JSONResponse:
        return _error(502, exc.error_code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        refused = await _refuse_unauthenticated(request)
        return refused or _error(422, "VALIDATION_ERROR", _describe(exc.errors()))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        refused = await _refuse_unauthenticated(request)
        code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
        return refused or _error(exc.status_code, code, str(exc.detail))

    # Unexpected exceptions are handled by `AuditMiddleware`, which answers 500 INTERNAL_ERROR
    # and logs only the exception type, so their messages reach neither client nor log.


def create_daemon_app(
    runtime: Runtime,
    credentials: CredentialStore,
    grants: GrantStore,
    approvals: ApprovalStore,
    audit: AuditLog,
    *,
    started_at: dt.datetime,
    clock: Callable[[], dt.datetime],
    presence: PresenceChecker,
    on_shutdown: Callable[[], None] | None = None,
    job_sleep: Callable[[float], None] | None = None,
    job_clock: Callable[[], float] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="findata daemon",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.daemon = DaemonState(
        runtime=runtime,
        credentials=credentials,
        grants=grants,
        approvals=approvals,
        audit=audit,
        started_at=started_at,
        clock=clock,
        on_shutdown=on_shutdown,
        presence=presence,
        job_sleep=job_sleep,
        job_clock=job_clock,
    )
    _install_error_handlers(app)
    app.add_middleware(AuditMiddleware, audit=audit)
    for router in ROUTERS:
        app.include_router(router)
    return app
