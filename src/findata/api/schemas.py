"""Request/response models (also the future agent-facing contract)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel


class ItemOut(BaseModel):
    id: str
    institution_name: str | None
    last_synced_at: dt.datetime | None


class AccountOut(BaseModel):
    id: str
    item_id: str
    name: str
    mask: str | None
    type: str | None
    subtype: str | None
    current_balance: Decimal | None
    available_balance: Decimal | None
    iso_currency_code: str | None


class TransactionOut(BaseModel):
    id: str
    account_id: str
    date: dt.date
    amount: Decimal
    iso_currency_code: str | None
    name: str
    merchant_name: str | None
    pending: bool
    category_primary: str | None
    category_detailed: str | None


class ExchangeIn(BaseModel):
    public_token: str


class SyncOut(BaseModel):
    item_id: str
    added: int
    modified: int
    removed: int
    status: str | None
    # False while Plaid is still loading older transactions: more will arrive on a later sync.
    history_complete: bool
    detail: str
