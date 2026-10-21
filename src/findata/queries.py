"""Read-only queries over the local database.

The one place that knows how to read Items, accounts and transactions. The HTTP API, the CLI
and (later) the MCP server all go through here instead of writing their own `select`s.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from findata.db import Account, Item, Transaction


@dataclass(frozen=True)
class ItemSummary:
    item: Item
    accounts: int
    transactions: int


def list_items(session: Session) -> list[Item]:
    return list(session.scalars(select(Item).order_by(Item.created_at)))


def list_item_summaries(session: Session) -> list[ItemSummary]:
    """Every Item with its account and transaction counts."""
    rows = session.execute(
        select(
            Item,
            select(func.count())
            .select_from(Account)
            .where(Account.item_id == Item.id)
            .scalar_subquery(),
            select(func.count())
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .where(Account.item_id == Item.id)
            .scalar_subquery(),
        ).order_by(Item.created_at)
    ).all()
    return [ItemSummary(item, accounts, txns) for item, accounts, txns in rows]


def find_items_by_prefix(session: Session, prefix: str) -> list[Item]:
    return list(session.scalars(select(Item).where(Item.id.startswith(prefix))))


def find_item_by_institution(session: Session, institution_id: str) -> Item | None:
    """The oldest Item linked to `institution_id`, if any."""
    return session.scalars(
        select(Item).where(Item.institution_id == institution_id).order_by(Item.created_at)
    ).first()


def list_accounts(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.name)))


def count_accounts(session: Session, item_id: str) -> int:
    return (
        session.scalar(select(func.count()).select_from(Account).where(Account.item_id == item_id))
        or 0
    )


def list_transactions(
    session: Session,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    account_id: str | None = None,
    category: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Transaction]:
    """Transactions newest first, optionally filtered by date range, account or category."""
    q = select(Transaction)
    if start:
        q = q.where(Transaction.date >= start)
    if end:
        q = q.where(Transaction.date <= end)
    if account_id:
        q = q.where(Transaction.account_id == account_id)
    if category:
        q = q.where(Transaction.category_primary == category)
    q = q.order_by(Transaction.date.desc(), Transaction.id).limit(limit).offset(offset)
    return list(session.scalars(q))
