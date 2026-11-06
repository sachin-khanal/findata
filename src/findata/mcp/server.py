"""MCP tools over DaemonClient (role `mcp`). Reads are open; writes need a human's grant.

Core modules are never imported here: every tool reaches data only through the daemon, which
refuses writes the user has not granted. Failures become tool errors that name the human step;
nothing is retried, queued or done another way.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from findata.client import DaemonClient
from findata.client.errors import DaemonClientError, DaemonNotRunning, DaemonRejected, DaemonTimeout

NOT_RUNNING = "The findata daemon is not running. Ask the user to run `findata start`."
TIMED_OUT = (
    "The findata daemon did not answer in time; it may still be working, and a write may "
    "already have taken effect. Do not retry; ask the user to check `findata status`."
)

_READ = ToolAnnotations(read_only_hint=True)
_SYNC = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _tool_error(exc: DaemonClientError) -> ToolError:
    if isinstance(exc, DaemonNotRunning):
        return ToolError(NOT_RUNNING)
    if isinstance(exc, DaemonTimeout):
        return ToolError(TIMED_OUT)
    if isinstance(exc, DaemonRejected):
        message = f"{exc.error_code}: {exc.detail}"
        request_id = exc.extra.get("request_id")
        if exc.error_code == "CONFIRMATION_REQUIRED" and request_id:
            message += (
                f" Ask the user to run `findata approve {request_id}`, then call "
                f"remove_item again with request_id={request_id}."
            )
        return ToolError(message)
    return ToolError(str(exc))


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


async def _run(fn, *args, **kwargs) -> Any:
    """Run one blocking client call off the event loop; map client errors to tool errors."""
    try:
        return _json(await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs)))
    except DaemonClientError as exc:
        raise _tool_error(exc) from None


def build_mcp_server(client: DaemonClient) -> MCPServer:
    mcp = MCPServer("findata", log_level="WARNING")  # httpx2 INFO logs carry query params

    @mcp.tool(annotations=_READ)
    async def get_status() -> dict[str, Any]:
        """Daemon status: version, Plaid environment, uptime."""
        return await _run(client.status)

    @mcp.tool(annotations=_READ)
    async def list_items() -> list[dict]:
        """Linked institutions (Items) with account and transaction counts."""
        return await _run(client.items)

    @mcp.tool(annotations=_READ)
    async def list_accounts() -> list[dict]:
        """Accounts across all linked institutions, with balances."""
        return await _run(client.accounts)

    @mcp.tool(annotations=_READ)
    async def list_transactions(
        start: dt.date | None = None,
        end: dt.date | None = None,
        account_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Transactions, newest first. Dates are ISO (YYYY-MM-DD); page with limit and offset."""
        return await _run(
            client.transactions,
            start=start,
            end=end,
            account_id=account_id,
            category=category,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(annotations=_SYNC)
    async def sync() -> list[dict]:
        """Pull new transactions from Plaid for every Item.

        Needs a human grant covering the `sync` operation: if refused, ask the user to run
        `findata grant mcp-write --ops sync` (grants expire on their own).
        """
        return await _run(client.sync)

    @mcp.tool(annotations=_REMOVE)
    async def remove_item(item_id: str, request_id: str | None = None) -> dict[str, Any]:
        """Permanently remove a linked institution (Item) and its local data.

        Needs a human grant covering `items.remove` (the user runs `findata grant mcp-write
        --ops items.remove`) plus a per-removal approval. The first call is refused with a
        request_id; ask the user to run `findata approve <request_id>`, then call again with
        that request_id.
        """
        await _run(client.remove_item, item_id, request_id=request_id)
        return {"removed": item_id}

    return mcp
