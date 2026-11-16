"""Read-only data routes: items (with counts), accounts, transactions."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from findata import queries
from findata.daemon.deps import SessionDep, require
from findata.wire import AccountOut, ItemOut, TransactionOut

router = APIRouter(prefix="/v1", dependencies=[require("read")])


def item_out(summary: queries.ItemSummary) -> ItemOut:
    item = summary.item
    return ItemOut(
        id=item.id,
        institution_name=item.institution_name,
        last_synced_at=item.last_synced_at,
        accounts=summary.accounts,
        transactions=summary.transactions,
    )


@router.get("/items", name="items.list")
def items(session: SessionDep) -> list[ItemOut]:
    return [item_out(s) for s in queries.list_item_summaries(session)]


@router.get("/accounts", name="accounts.list")
def accounts(session: SessionDep) -> list[AccountOut]:
    return [
        AccountOut.model_validate(r, from_attributes=True) for r in queries.list_accounts(session)
    ]


@router.get("/transactions", name="transactions.list")
def transactions(
    session: SessionDep,
    start: dt.date | None = None,
    end: dt.date | None = None,
    account_id: str | None = None,
    category: str | None = Query(None, description="personal_finance_category.primary"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[TransactionOut]:
    rows = queries.list_transactions(
        session,
        start=start,
        end=end,
        account_id=account_id,
        category=category,
        limit=limit,
        offset=offset,
    )
    return [TransactionOut.model_validate(r, from_attributes=True) for r in rows]
