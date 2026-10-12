"""Transactions sync: pull deltas from /transactions/sync and apply them to SQLite.

Follows Plaid's recommended pattern:
  * page until has_more is False, accumulating added/modified/removed in memory;
  * on TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION, discard and restart from the
    cursor we started with;
  * only after the full delta is collected, apply it and persist next_cursor in one
    DB transaction, so a crash never leaves the cursor ahead of the data.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from findata.db import Account, Item, Transaction
from findata.plaid_gateway import PlaidError, PlaidGateway
from findata.security import FieldCipher

log = logging.getLogger(__name__)

MUTATION_ERROR = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
MAX_RESTARTS = 3


@dataclass
class SyncDelta:
    added: list[dict[str, Any]] = field(default_factory=list)
    modified: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_cursor: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class SyncResult:
    item_id: str
    added: int
    modified: int
    removed: int
    status: str | None


def fetch_delta(gateway: PlaidGateway, access_token: str, cursor: str | None) -> SyncDelta:
    for attempt in range(MAX_RESTARTS + 1):
        delta = SyncDelta(next_cursor=cursor)
        try:
            while True:
                page = gateway.transactions_sync(access_token, delta.next_cursor)
                delta.added.extend(page.get("added", []))
                delta.modified.extend(page.get("modified", []))
                delta.removed.extend(page.get("removed", []))
                for acct in page.get("accounts", []):
                    delta.accounts[acct["account_id"]] = acct
                delta.next_cursor = page["next_cursor"]
                delta.status = page.get("transactions_update_status")
                if not page.get("has_more"):
                    return delta
        except PlaidError as exc:
            if exc.error_code != MUTATION_ERROR or attempt == MAX_RESTARTS:
                raise
            log.info("Data changed mid-pagination; restarting from original cursor")
    raise AssertionError("unreachable")


def _as_date(value: Any) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _upsert_account(session: Session, item_id: str, a: dict[str, Any]) -> None:
    bal = a.get("balances") or {}
    acct = session.get(Account, a["account_id"]) or Account(id=a["account_id"], item_id=item_id)
    acct.name = a.get("name") or "Account"
    acct.official_name = a.get("official_name")
    acct.mask = a.get("mask")
    acct.type = str(a["type"]) if a.get("type") is not None else None
    acct.subtype = str(a["subtype"]) if a.get("subtype") is not None else None
    acct.current_balance = bal.get("current")
    acct.available_balance = bal.get("available")
    acct.iso_currency_code = bal.get("iso_currency_code")
    session.add(acct)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dt.date | dt.datetime):
        return obj.isoformat()
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)  # Plaid enum-like model values


def token_context(item_id: str) -> str:
    """Associated data binding an access-token ciphertext to its Item."""
    return f"items.access_token:{item_id}"


def raw_context(transaction_id: str) -> str:
    """Associated data binding a raw-payload ciphertext to its transaction."""
    return f"transactions.raw:{transaction_id}"


def _upsert_transaction(session: Session, cipher: FieldCipher, t: dict[str, Any]) -> None:
    pfc = t.get("personal_finance_category") or {}
    txn = session.get(Transaction, t["transaction_id"]) or Transaction(id=t["transaction_id"])
    txn.account_id = t["account_id"]
    txn.amount = t["amount"]
    txn.iso_currency_code = t.get("iso_currency_code")
    txn.date = _as_date(t["date"])
    txn.authorized_date = _as_date(t.get("authorized_date"))
    txn.name = t.get("name") or ""
    txn.merchant_name = t.get("merchant_name")
    txn.pending = bool(t.get("pending"))
    txn.pending_transaction_id = t.get("pending_transaction_id")
    txn.payment_channel = str(t["payment_channel"]) if t.get("payment_channel") else None
    txn.category_primary = pfc.get("primary")
    txn.category_detailed = pfc.get("detailed")
    txn.raw_enc = cipher.encrypt(json.dumps(_jsonable(t)), raw_context(txn.id))
    session.add(txn)


def apply_delta(session: Session, cipher: FieldCipher, item: Item, delta: SyncDelta) -> None:
    for acct in delta.accounts.values():
        _upsert_account(session, item.id, acct)
    session.flush()  # accounts must exist before transactions reference them
    for t in [*delta.added, *delta.modified]:
        _upsert_transaction(session, cipher, t)
    removed_ids = [r["transaction_id"] for r in delta.removed]
    if removed_ids:
        session.execute(delete(Transaction).where(Transaction.id.in_(removed_ids)))
    item.sync_cursor = delta.next_cursor
    item.last_synced_at = dt.datetime.now(dt.UTC)


def sync_item(
    session: Session, gateway: PlaidGateway, cipher: FieldCipher, item: Item
) -> SyncResult:
    access_token = cipher.decrypt(item.access_token_enc, token_context(item.id))
    delta = fetch_delta(gateway, access_token, item.sync_cursor)
    try:
        apply_delta(session, cipher, item, delta)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return SyncResult(
        item.id, len(delta.added), len(delta.modified), len(delta.removed), delta.status
    )


def sync_all(session: Session, gateway: PlaidGateway, cipher: FieldCipher) -> list[SyncResult]:
    items = session.scalars(select(Item)).all()
    return [sync_item(session, gateway, cipher, item) for item in items]


def link_item(
    session: Session, gateway: PlaidGateway, cipher: FieldCipher, public_token: str
) -> Item:
    """Exchange a public token and store the resulting Item with an encrypted access token."""
    item_id, access_token = gateway.exchange_public_token(public_token)
    meta = gateway.get_item(access_token)
    item = session.get(Item, item_id) or Item(id=item_id)
    item.access_token_enc = cipher.encrypt(access_token, token_context(item_id))
    item.institution_id = meta.get("institution_id")
    item.institution_name = meta.get("institution_name")
    session.add(item)
    session.commit()
    return item
