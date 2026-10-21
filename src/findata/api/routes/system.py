"""Unauthenticated routes: the web UI, favicon and health check."""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> str:
    state = request.app.state
    html = files("findata.web").joinpath("index.html").read_text()
    # Same-origin page only; other origins can't read this response (no CORS).
    return html.replace("__FINDATA_TOKEN__", state.api_token).replace(
        "__PLAID_ENV__", state.runtime.settings.plaid_env
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)  # no icon; avoids a 404 in browser consoles


@router.get("/api/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "plaid_env": request.app.state.runtime.settings.plaid_env}
