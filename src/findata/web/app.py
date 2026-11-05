"""Web front end (FastAPI): serves the page and forwards every call to the daemon.

It holds no key, database or Plaid access: everything comes from a `DaemonClient` with the
`web` role. Binds to 127.0.0.1 only; every /api data route needs the token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from findata.client import (
    DaemonClient,
    DaemonClientError,
    DaemonNotRunning,
    DaemonRejected,
    DaemonTimeout,
)
from findata.web import routes


def _error(status: int, error_code: str, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error_code": error_code, "detail": detail, **extra}
    )


def create_web_app(client: DaemonClient, api_token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        client.close()
        await client.aclose()

    app = FastAPI(
        title="findata", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan
    )
    app.state.client = client
    app.state.api_token = api_token
    # Blocks DNS-rebinding attacks against the localhost server.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(DaemonNotRunning)
    async def _not_running(_: Request, exc: DaemonNotRunning) -> JSONResponse:
        return _error(503, "DAEMON_NOT_RUNNING", str(exc))

    @app.exception_handler(DaemonTimeout)
    async def _timeout(_: Request, exc: DaemonTimeout) -> JSONResponse:
        return _error(504, "DAEMON_TIMEOUT", str(exc))

    @app.exception_handler(DaemonRejected)
    async def _rejected(_: Request, exc: DaemonRejected) -> JSONResponse:
        return _error(exc.status, exc.error_code, exc.detail, **exc.extra)

    @app.exception_handler(DaemonClientError)
    async def _not_configured(_: Request, exc: DaemonClientError) -> JSONResponse:
        return _error(503, "NOT_CONFIGURED", str(exc))  # e.g. no credential file yet

    app.include_router(routes.router)
    return app
