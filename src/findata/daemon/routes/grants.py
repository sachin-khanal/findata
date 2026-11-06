"""Human-only controls over the agent: write grants and approvals of pending requests.

Creating a grant and approving a request widen what the agent may do, so the user confirms
each at the Mac first (`presence_gate`). Revoking only ever narrows it and is never asked.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Response

from findata.daemon.deps import DaemonError, PresenceDep, SessionDep, StateDep, require
from findata.daemon.grants import GRANTABLE
from findata.daemon.routes.items import describe_item
from findata.db import Item
from findata.wire import GrantIn, GrantOut

router = APIRouter(prefix="/v1", dependencies=[require("admin")])

# How the grant prompt names each operation, in the order it lists them.
_PHRASES = {"sync": "sync", "link": "link banks", "items.remove": "remove items"}


def _minutes(seconds: int) -> str:
    n = math.ceil(seconds / 60)
    return "1 minute" if n == 1 else f"{n} minutes"


def _grant_reason(operations: set[str], ttl_s: int) -> str:
    # An operation without a phrase fails here rather than going unmentioned in the prompt.
    phrases = [_PHRASES[op] for op in sorted(operations, key=list(_PHRASES).index)]
    listed = phrases[0] if len(phrases) == 1 else f"{', '.join(phrases[:-1])} and {phrases[-1]}"
    return f"allow the findata agent to {listed} for {_minutes(ttl_s)}"


@router.post("/grants", name="grants.create")
def create_grant(body: GrantIn, state: StateDep, confirm_presence: PresenceDep) -> GrantOut:
    operations = set(body.operations)
    if not operations <= GRANTABLE:  # checked before the prompt: never ask about a bad grant
        raise DaemonError(
            422, "VALIDATION_ERROR", f"operations must be a non-empty subset of {sorted(GRANTABLE)}"
        )
    confirm_presence(_grant_reason(operations, body.ttl_s))
    return state.grants.grant(operations, body.ttl_s)


@router.get("/grants", name="grants.get")
def current_grant(state: StateDep) -> GrantOut | None:
    return state.grants.current()


@router.delete("/grants", name="grants.revoke", status_code=204)
def revoke_grant(state: StateDep) -> Response:
    state.grants.revoke()
    return Response(status_code=204)


@router.get("/approvals", name="approvals.list")
def pending_approvals(state: StateDep) -> list[dict[str, Any]]:
    return state.approvals.pending()


def _no_pending_request() -> DaemonError:
    return DaemonError(
        404, "NOT_FOUND", "No pending request has that id; it may have expired or been used."
    )


@router.post("/approvals/{request_id}", name="approvals.approve", status_code=204)
def approve(
    request_id: str, state: StateDep, session: SessionDep, confirm_presence: PresenceDep
) -> Response:
    pending = next((p for p in state.approvals.pending() if p["request_id"] == request_id), None)
    if pending is None:
        raise _no_pending_request()
    item = session.get(Item, pending["subject"])  # every pending request is an item removal
    confirm_presence(f"approve the agent's request to remove {describe_item(item)}")
    try:
        state.approvals.approve(request_id)
    except KeyError:  # it expired while the prompt was up
        raise _no_pending_request() from None
    return Response(status_code=204)
