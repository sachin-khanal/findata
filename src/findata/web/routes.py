"""The page, favicon, health and the /api data routes, each forwarded to the daemon."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from importlib.resources import files

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from findata.client import DaemonClientError, DaemonRejected
from findata.web.deps import ClientDep, auth
from findata.wire import AccountOut, ExchangeIn, ItemOut, SyncOut, TransactionOut

router = APIRouter()
data = APIRouter(prefix="/api", dependencies=auth)


def _start_history(client: ClientDep, item_id: str) -> None:
    """Have the daemon wait for the Item's history. A job already running is fine: its events
    reach the page anyway."""
    try:
        client.start_history(item_id)
    except DaemonRejected as exc:
        if exc.error_code != "JOB_ALREADY_RUNNING":
            raise


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, client: ClientDep) -> str:
    try:
        plaid_env = client.status().plaid_env
    except DaemonClientError:
        plaid_env = ""  # the page still loads and says the daemon is not running
    html = files("findata.web").joinpath("index.html").read_text()
    # Same-origin page only; other origins can't read this response (no CORS).
    return html.replace("__FINDATA_TOKEN__", request.app.state.api_token).replace(
        "__PLAID_ENV__", plaid_env
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)  # no icon; avoids a 404 in browser consoles


@router.get("/api/health")
def health(client: ClientDep) -> dict[str, str]:
    return {"status": "ok", "plaid_env": client.status().plaid_env}


@data.get("/items")
def items(client: ClientDep) -> list[ItemOut]:
    return client.items()


@data.get("/accounts")
def accounts(client: ClientDep) -> list[AccountOut]:
    return client.accounts()


@data.get("/transactions")
def transactions(
    client: ClientDep,
    start: dt.date | None = None,
    end: dt.date | None = None,
    account_id: str | None = None,
    category: str | None = Query(None, description="personal_finance_category.primary"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[TransactionOut]:
    return client.transactions(
        start=start,
        end=end,
        account_id=account_id,
        category=category,
        limit=limit,
        offset=offset,
    )


@data.post("/sync")
def sync(client: ClientDep) -> list[SyncOut]:
    results = client.sync()
    loading = next((r for r in results if not r.history_complete), None)
    if loading is not None:
        _start_history(client, loading.item_id)
    return results


@data.post("/link/token")
def link_token(client: ClientDep) -> dict[str, str]:
    return {"link_token": client.link_token()}


@data.post("/link/exchange")
def exchange(body: ExchangeIn, client: ClientDep) -> ItemOut:
    item = client.exchange(body.public_token)
    _start_history(client, item.id)
    return item


@data.get("/events")
async def events(client: ClientDep, after: int | None = Query(None, ge=0)) -> StreamingResponse:
    """The daemon's events as NDJSON, keep-alives included. Opened before the response starts,
    so a down or refusing daemon is an error status; closed when the browser goes away, which
    frees the daemon's stream slot."""
    upstream = await client.aevents(after, keepalives=True)

    async def relay() -> AsyncIterator[str]:
        try:
            async for event in upstream:
                yield json.dumps(event, separators=(",", ":")) + "\n"
        except DaemonClientError:
            return  # the daemon went away mid-stream: the page sees the stream end
        finally:
            await upstream.aclose()

    return StreamingResponse(relay(), media_type="application/x-ndjson")


router.include_router(data)
