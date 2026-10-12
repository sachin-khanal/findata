from decimal import Decimal

import pytest
from sqlalchemy import select, text

from findata.db import Account, Item, Transaction
from findata.plaid_gateway import PlaidError
from findata.sync import MUTATION_ERROR, link_item, sync_item
from tests.conftest import ACCESS_TOKEN, FakeGateway, account, txn


def page(next_cursor, added=(), modified=(), removed=(), has_more=False, accounts=None):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": r, "account_id": "acc_1"} for r in removed],
        "accounts": accounts if accounts is not None else [account()],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
    }


@pytest.fixture
def item(session, cipher):
    return link_item(session, FakeGateway(), cipher, "public-sandbox-ok")


def test_link_item_stores_only_encrypted_token(session, cipher, item):
    assert item.institution_name == "First Platypus Bank"
    raw = session.execute(text("SELECT access_token_enc FROM items")).scalar_one()
    assert ACCESS_TOKEN not in raw
    assert cipher.decrypt(raw) == ACCESS_TOKEN


def test_link_item_bad_public_token(session, cipher):
    with pytest.raises(PlaidError) as ei:
        link_item(session, FakeGateway(), cipher, "nope")
    assert ei.value.error_code == "INVALID_PUBLIC_TOKEN"


def test_initial_sync_paginates_and_stores(session, cipher, item):
    gw = FakeGateway(
        {
            None: page("c1", added=[txn("t1"), txn("t2")], has_more=True),
            "c1": page("c2", added=[txn("t3", amount=-2500.0, name="Payroll")]),
        }
    )
    r = sync_item(session, gw, cipher, item)
    assert (r.added, r.modified, r.removed) == (3, 0, 0)
    assert gw.sync_calls == [None, "c1"]
    assert session.get(Item, "item_1").sync_cursor == "c2"
    assert session.get(Account, "acc_1").current_balance == Decimal("100.0")
    t3 = session.get(Transaction, "t3")
    assert t3.amount == Decimal("-2500.0") and t3.category_primary == "FOOD_AND_DRINK"
    assert t3.raw["date"] == "2026-09-01"


def test_incremental_sync_modifies_and_removes(session, cipher, item):
    sync_item(
        session,
        FakeGateway({None: page("c1", added=[txn("t1"), txn("t2", pending=True)])}),
        cipher,
        item,
    )
    gw = FakeGateway({"c1": page("c2", modified=[txn("t1", amount=13.0)], removed=["t2"])})
    r = sync_item(session, gw, cipher, item)
    assert (r.added, r.modified, r.removed) == (0, 1, 1)
    assert session.get(Transaction, "t1").amount == Decimal("13.0")
    assert session.get(Transaction, "t2") is None
    assert gw.sync_calls == ["c1"]


def test_mutation_during_pagination_restarts_from_original_cursor(session, cipher, item):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return PlaidError(MUTATION_ERROR, "changed", 400)
        return page("c2", added=[txn("t2")])

    gw = FakeGateway({None: page("c1", added=[txn("t1")], has_more=True), "c1": flaky})
    r = sync_item(session, gw, cipher, item)
    assert gw.sync_calls == [None, "c1", None, "c1"]
    assert r.added == 2  # first attempt's t1 was discarded, not double-counted
    assert session.get(Item, "item_1").sync_cursor == "c2"


def test_failed_sync_does_not_advance_cursor(session, cipher, item):
    gw = FakeGateway(
        {
            None: page("c1", added=[txn("t1")], has_more=True),
            "c1": PlaidError("ITEM_LOGIN_REQUIRED", "relink", 400),
        }
    )
    with pytest.raises(PlaidError):
        sync_item(session, gw, cipher, item)
    session.expire_all()
    assert session.get(Item, "item_1").sync_cursor is None
    assert session.scalars(select(Transaction)).all() == []
