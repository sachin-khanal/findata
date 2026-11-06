"""Trigger a transactions sync of every linked Item, one sync at a time."""

from __future__ import annotations

from fastapi import APIRouter

from findata.daemon.deps import PLAID_WRITE_LOCK, GatewayDep, SessionDep, StateDep, require
from findata.daemon.events import EventBus
from findata.sync import SyncResult, sync_all
from findata.wire import SyncOut

router = APIRouter(prefix="/v1")


def publish_sync_completed(events: EventBus, r: SyncResult) -> None:
    """One Item's sync went through (from this route or a job's pass): counts only."""
    events.publish(
        "sync_completed",
        item_id=r.item_id,
        added=r.added,
        modified=r.modified,
        removed=r.removed,
        history_complete=r.history_complete,
    )


# A plain `def`: the sync blocks on Plaid and the database, so FastAPI runs it in its thread
# pool, and the lock makes a second request wait for the first instead of syncing alongside it.
@router.post("/sync", name="sync", dependencies=[require("sync")])
def sync(session: SessionDep, gateway: GatewayDep, state: StateDep) -> list[SyncOut]:
    with PLAID_WRITE_LOCK:
        results = sync_all(session, gateway, state.runtime.cipher)
    for r in results:
        publish_sync_completed(state.events, r)
    return [
        SyncOut(**r.__dict__, history_complete=r.history_complete, detail=r.detail) for r in results
    ]
