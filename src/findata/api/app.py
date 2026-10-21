"""Local HTTP API (FastAPI). Binds to 127.0.0.1 only; every /api data route needs the token."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.middleware.trustedhost import TrustedHostMiddleware

from findata.api.routes import data, link, sync, system
from findata.config import Settings
from findata.plaid_gateway import PlaidError, PlaidGateway
from findata.runtime import build_runtime
from findata.security import load_or_create_api_token


def create_app(
    settings: Settings | None = None,
    gateway: PlaidGateway | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    runtime = build_runtime(settings, gateway=gateway, engine=engine)

    app = FastAPI(title="findata", version="0.1.0", docs_url="/docs", redoc_url=None)
    app.state.runtime = runtime
    app.state.api_token = load_or_create_api_token(runtime.settings.api_token_path)
    # Blocks DNS-rebinding attacks against the localhost server.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(PlaidError)
    async def _plaid_error(_: Request, exc: PlaidError) -> JSONResponse:
        return JSONResponse(
            status_code=502, content={"error_code": exc.error_code, "detail": str(exc)}
        )

    for module in (system, link, sync, data):
        app.include_router(module.router)
    return app
