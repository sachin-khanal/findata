"""Remove a linked Item: revoke it at Plaid and delete it and its data locally.

An agent (mcp) needs a grant for `items.remove` *and* a human's approval of each removal:
its first call creates a pending request and is refused with CONFIRMATION_REQUIRED and the
request's id; a human approves that id (`POST /v1/approvals/{id}`); the agent retries with
`?request_id=` and the approval is used up. An agent can never remove `local_only`: that skips
revoking the Item at Plaid, which is not what the human approved.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from findata.daemon.deps import (
    PLAID_WRITE_LOCK,
    DaemonError,
    RoleDep,
    SessionDep,
    StateDep,
    get_gateway,
    require,
)
from findata.daemon.grants import ApprovalStore
from findata.db import Item
from findata.linking import remove_item

REMOVE = "items.remove"

router = APIRouter(prefix="/v1")


def _pending_request(approvals: ApprovalStore, item_id: str) -> str:
    """The open request to remove `item_id`, or a new one (retries never pile up requests)."""
    for p in approvals.pending():
        if p["operation"] == REMOVE and p["subject"] == item_id:
            return p["request_id"]
    return approvals.request(REMOVE, item_id)


@router.delete("/items/{item_id}", name=REMOVE, status_code=204, dependencies=[require(REMOVE)])
def remove(
    item_id: str,
    role: RoleDep,
    session: SessionDep,
    state: StateDep,
    local_only: bool = False,
    request_id: str | None = None,
) -> Response:
    if role == "mcp" and local_only:  # before any approval is created or used
        raise DaemonError(
            403,
            "FORBIDDEN",
            "Local-only removal skips revoking the item at Plaid, so only a human can do it "
            "(`findata items rm <id> --local-only`). An agent may only request a normal removal.",
        )
    item = session.get(Item, item_id)
    if item is None:
        raise DaemonError(404, "NOT_FOUND", f"No linked item has id {item_id!r}.")
    if role == "mcp" and not (request_id and state.approvals.consume(request_id, REMOVE, item_id)):
        rid = _pending_request(state.approvals, item_id)
        raise DaemonError(
            403,
            "CONFIRMATION_REQUIRED",
            f"Removing an item needs a human to approve each removal. Ask the user to run "
            f"`findata approve {rid}` (or approve it in the web UI), then retry with "
            f"request_id={rid}.",
            request_id=rid,
        )
    gateway = None if local_only else get_gateway(state)
    with PLAID_WRITE_LOCK:
        remove_item(session, gateway, state.runtime.cipher, item, local_only=local_only)
    return Response(status_code=204)
