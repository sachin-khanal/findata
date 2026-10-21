"""Re-export of the wire models, which now live in `findata.wire` (removed with `api/`)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from findata.wire import AccountOut, ExchangeIn, SyncOut, TransactionOut


class ItemOut(BaseModel):
    """Legacy shape without counts: the ORM Item's `accounts` is a relationship, not an int."""

    id: str
    institution_name: str | None
    last_synced_at: dt.datetime | None


__all__ = ["AccountOut", "ExchangeIn", "ItemOut", "SyncOut", "TransactionOut"]
