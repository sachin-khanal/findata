"""Trigger a transactions sync."""

from __future__ import annotations

from fastapi import APIRouter

from findata.api.deps import GatewayDep, RuntimeDep, SessionDep, auth
from findata.api.schemas import SyncOut
from findata.sync import sync_all

router = APIRouter(prefix="/api", dependencies=auth)


@router.post("/sync")
def sync(session: SessionDep, gw: GatewayDep, runtime: RuntimeDep) -> list[SyncOut]:
    return [
        SyncOut(**r.__dict__, history_complete=r.history_complete, detail=r.detail)
        for r in sync_all(session, gw, runtime.cipher)
    ]
