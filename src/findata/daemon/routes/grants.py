"""Human-only controls over the agent: write grants and approvals of pending requests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from findata.daemon.deps import DaemonError, StateDep, require
from findata.wire import GrantIn, GrantOut

router = APIRouter(prefix="/v1", dependencies=[require("admin")])


@router.post("/grants", name="grants.create")
def create_grant(body: GrantIn, state: StateDep) -> GrantOut:
    try:
        return state.grants.grant(set(body.operations), body.ttl_s)
    except ValueError as exc:  # an operation that cannot be granted
        raise DaemonError(422, "VALIDATION_ERROR", str(exc)) from None


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


@router.post("/approvals/{request_id}", name="approvals.approve", status_code=204)
def approve(request_id: str, state: StateDep) -> Response:
    try:
        state.approvals.approve(request_id)
    except KeyError:
        raise DaemonError(
            404, "NOT_FOUND", "No pending request has that id; it may have expired or been used."
        ) from None
    return Response(status_code=204)
