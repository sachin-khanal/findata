"""The page, favicon, health and the /api data routes, each forwarded to the daemon."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from importlib.resources import files

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from findata.client import DaemonClient, DaemonClientError, DaemonRejected
from findata.web.deps import OPEN_LINK, ClientDep, auth
from findata.web.session import WebSessions
from findata.wire import (
    AccountOut,
    ExchangeIn,
    GrantIn,
    GrantOut,
    ItemOut,
    SyncOut,
    TransactionOut,
)

log = logging.getLogger(__name__)
router = APIRouter()


class PendingApprovalOut(BaseModel):
    request_id: str
    operation: str
    item_id: str
    institution: str | None
    created_at: dt.datetime


class LaunchIn(BaseModel):
    launch: str


data = APIRouter(prefix="/api", dependencies=auth)


def _start_history(client: DaemonClient, item_id: str) -> None:
    """Have the daemon wait for the Item's history. Never fails the request: the exchange or
    sync it follows has already been applied, and a job already running (409) reaches the page
    through its events anyway. Any other failure is logged and the page's next Sync retries."""
    try:
        client.start_history(item_id)
    except DaemonRejected as exc:
        if exc.error_code != "JOB_ALREADY_RUNNING":
            log.warning("Could not start a history job (%s, HTTP %s).", exc.error_code, exc.status)
    except DaemonClientError as exc:
        log.warning("Could not start a history job: %s", type(exc).__name__)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(client: ClientDep) -> str:
    try:
        status = client.status()
        plaid_env, presence = status.plaid_env, status.user_presence
    except DaemonClientError:
        plaid_env, presence = "", ""  # the page still loads and says the daemon is not running
    html = files("findata.web").joinpath("index.html").read_text()
    # The page carries no credential: a session starts from the launch link's fragment.
    return html.replace("__PLAID_ENV__", plaid_env).replace("__USER_PRESENCE__", presence)


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)  # no icon; avoids a 404 in browser consoles


@router.post("/api/session")
def start_session(body: LaunchIn, request: Request) -> Response:
    """Trade the launch secret for a session: an HttpOnly cookie plus a request key in the body."""
    pair = request.app.state.sessions.start(body.launch)
    if pair is None:
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "LAUNCH_LINK_INVALID",
                "detail": f"The launch link is not valid. {OPEN_LINK}",
            },
        )
    session_id, request_key = pair
    response = JSONResponse({"request_key": request_key})
    WebSessions.set_cookie(response, session_id)
    return response


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


@data.get("/grants")
def current_grant(client: ClientDep) -> GrantOut | None:
    return client.current_grant()


@data.post("/grants")
def create_grant(body: GrantIn, client: ClientDep) -> GrantOut:
    return client.grant(body.operations, body.ttl_s)  # the daemon asks for Touch ID


@data.delete("/grants", status_code=204)
def revoke_grant(client: ClientDep) -> Response:
    client.revoke()
    return Response(status_code=204)


@data.get("/approvals")
def approvals(client: ClientDep) -> list[PendingApprovalOut]:
    """Pending requests; the bank's name lets the page say what is being removed."""
    return [
        PendingApprovalOut(
            request_id=p["request_id"],
            operation=p["operation"],
            item_id=p["subject"],
            institution=p.get("institution"),
            created_at=p["created_at"],
        )
        for p in client.approvals()
    ]


@data.post("/approvals/{request_id}", status_code=204)
def approve(request_id: str, client: ClientDep) -> Response:
    client.approve(request_id)  # the daemon asks for Touch ID
    return Response(status_code=204)


@data.get("/events")
async def events(
    request: Request, client: ClientDep, after: int | None = Query(None, ge=0)
) -> StreamingResponse:
    """The daemon's events as NDJSON, keep-alives included. Opened before the response starts,
    so a down or refusing daemon is an error status; closed when the browser goes away, which
    frees the daemon's stream slot."""
    upstream = await client.aevents(after, keepalives=True)
    stop = asyncio.ensure_future(request.app.state.stopping.wait())

    async def relay() -> AsyncIterator[str]:
        pending: asyncio.Future | None = None
        try:
            while True:
                pending = asyncio.ensure_future(anext(upstream))
                await asyncio.wait({pending, stop}, return_when=asyncio.FIRST_COMPLETED)
                if not pending.done():
                    return  # the server is shutting down
                event = pending.result()
                yield json.dumps(event, separators=(",", ":")) + "\n"
        except StopAsyncIteration:
            return
        except DaemonClientError:
            return  # the daemon went away mid-stream: the page sees the stream end
        finally:
            stop.cancel()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await upstream.aclose()

    return StreamingResponse(relay(), media_type="application/x-ndjson")


router.include_router(data)
