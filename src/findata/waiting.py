"""Wait for a freshly linked Item's transaction history to finish loading."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from findata import queries
from findata.db import Item
from findata.plaid_gateway import PlaidGateway
from findata.security import FieldCipher
from findata.sync import SyncResult, sync_item


@dataclass(frozen=True)
class WaitOutcome:
    """Result of waiting for an Item's history to finish loading, summed over every poll."""

    item_id: str
    added: int
    modified: int
    removed: int
    status: str | None
    polls: int
    elapsed: float
    settled: bool = False

    @property
    def history_complete(self) -> bool:
        """Plaid said complete AND the last pass changed nothing (see wait_for_history)."""
        return self.settled


def wait_for_history(
    session: Session,
    gateway: PlaidGateway,
    cipher: FieldCipher,
    item: Item,
    *,
    timeout: float = 120.0,
    interval: float = 5.0,
    on_poll: Callable[[int, SyncResult, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> WaitOutcome:
    """Sync `item` repeatedly until Plaid reports its full history, or `timeout` passes.

    A freshly linked Item reports INITIAL_UPDATE_COMPLETE with few or no transactions; the
    rest arrives later, so a single sync is not "done". Plaid can also report
    HISTORICAL_UPDATE_COMPLETE a pass before the last rows land (and keep editing recent ones),
    so the status alone is not trusted: the wait ends only on a pass that reports complete and
    adds, modifies and removes nothing, once the Item's accounts are known. The wait is bounded
    and never raises on timeout: the caller inspects `history_complete` and can simply sync
    again later.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    start = clock()
    added = modified = removed = polls = 0
    while True:
        result = sync_item(session, gateway, cipher, item)
        polls += 1
        added, modified, removed = (
            added + result.added,
            modified + result.modified,
            removed + result.removed,
        )
        elapsed = clock() - start
        if on_poll:
            on_poll(polls, result, elapsed)
        quiet = not (result.added or result.modified or result.removed)
        has_accounts = queries.count_accounts(session, item.id) > 0
        settled = result.history_complete and quiet and has_accounts
        if settled or elapsed + interval > timeout:
            return WaitOutcome(
                item.id, added, modified, removed, result.status, polls, elapsed, settled
            )
        sleep(interval)
