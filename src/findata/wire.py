"""Wire models shared by the daemon and its clients (agent-facing contract)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

MAX_GRANT_TTL_S = 86_400  # a write grant lasts at most a day


class ItemOut(BaseModel):
    id: str
    institution_name: str | None
    last_synced_at: dt.datetime | None
    accounts: int = 0
    transactions: int = 0


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


class StatusOut(BaseModel):
    version: str
    plaid_env: str
    schema_revision: str | None
    started_at: dt.datetime
    uptime_s: float


class GrantIn(BaseModel):
    operations: list[str] = Field(min_length=1)
    ttl_s: int = Field(gt=0, le=MAX_GRANT_TTL_S)


class GrantOut(BaseModel):
    operations: list[str]
    expires_at: dt.datetime


class SandboxLinkIn(BaseModel):
    username: str | None = None
    new: bool = False
    wait: bool = True
    timeout: float = 120.0
    interval: float = 2.0


class JobOut(BaseModel):
    id: str
    state: Literal["running", "done", "failed"]
    events: list[dict] = []
    result: dict | None = None
