"""`sync`: pull new/changed/removed transactions for every linked item."""

from __future__ import annotations

from findata.cli.output import JsonOpt, emit, plaid_rejected
from findata.cli.session import connect
from findata.plaid_gateway import PlaidError
from findata.sync import sync_all


def sync(as_json: bool = JsonOpt) -> None:
    """Pull new/changed/removed transactions for every linked item."""
    rt = connect(as_json, need_plaid=True)
    try:
        with rt.session_factory() as session:
            results = sync_all(session, rt.gateway, rt.cipher)
    except PlaidError as exc:
        plaid_rejected(as_json, exc)
    if not results:
        emit(
            as_json,
            "No linked items yet. Run `findata sandbox-link` or link one in the "
            "web UI (`findata serve`).",
            event="synced",
            items=[],
        )
        return
    for r in results:
        emit(
            as_json,
            f"{r.item_id}: +{r.added} ~{r.modified} -{r.removed}. {r.detail}",
            event="item",
            item_id=r.item_id,
            added=r.added,
            modified=r.modified,
            removed=r.removed,
            status=r.status,
            history_complete=r.history_complete,
        )
    if not all(r.history_complete for r in results):
        emit(
            as_json,
            "Some items are still loading history; run `findata sync` again shortly.",
            event="note",
            history_complete=False,
        )
