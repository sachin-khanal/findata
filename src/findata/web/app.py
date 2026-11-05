"""Web front end (FastAPI): serves the page and forwards every call to the daemon.

It holds no key, database or Plaid access: everything comes from a `DaemonClient` with the
`web` role. Binds to 127.0.0.1 only; every /api data route needs a browser session
(started from the launch link `serve web` prints).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
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
from findata.web.deps import OPEN_LINK, BrowserSessionRequired
from findata.web.session import WebSessions


def _error(status: int, error_code: str, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error_code": error_code, "detail": detail, **extra}
    )


def create_web_app(client: DaemonClient, sessions: WebSessions | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        client.close()
        await client.aclose()

    app = FastAPI(
        title="findata", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan
    )
    app.state.client = client
    app.state.sessions = sessions or WebSessions()
    app.state.stopping = asyncio.Event()  # set as shutdown begins: open event relays end
    # Blocks DNS-rebinding attacks against the localhost server.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(BrowserSessionRequired)
    async def _no_session(_: Request, __: BrowserSessionRequired) -> JSONResponse:
        return _error(401, "NO_BROWSER_SESSION", f"No browser session. {OPEN_LINK}")

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


class WebServer(uvicorn.Server):
    """Ends the open /api/events relays as shutdown begins. Uvicorn waits for every open
    response to finish, and a relay never would."""

    def __init__(
        self, app: FastAPI, on_listening: Callable[[], None] | None = None, **config: object
    ) -> None:
        super().__init__(uvicorn.Config(app, **config))
        self._stopping = app.state.stopping
        self._on_listening = on_listening

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self._on_listening is not None and not self.should_exit:
            self._on_listening()  # the port is bound: the launch link can now be followed

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._stopping.set()
        await super().shutdown(sockets=sockets)
