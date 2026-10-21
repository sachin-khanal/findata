"""Read-only data routes: items, accounts, transactions."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from findata import queries
from findata.api.deps import SessionDep, auth
from findata.api.schemas import AccountOut, ItemOut, TransactionOut

router = APIRouter(prefix="/api", dependencies=auth)


@router.get("/items")
def items(session: SessionDep) -> list[ItemOut]:
    return [ItemOut.model_validate(r, from_attributes=True) for r in queries.list_items(session)]


@router.get("/accounts")
def accounts(session: SessionDep) -> list[AccountOut]:
    return [
        AccountOut.model_validate(r, from_attributes=True) for r in queries.list_accounts(session)
    ]


@router.get("/transactions")
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
