"""Read the audit log (humans only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from findata.daemon.deps import StateDep, require

router = APIRouter(prefix="/v1", dependencies=[require("admin")])


@router.get("/audit", name="audit.list")
def recent(state: StateDep, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return state.audit.recent(limit)
