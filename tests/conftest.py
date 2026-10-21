from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from findata.config import Settings
from findata.db import make_engine, make_session_factory
from findata.plaid_gateway import PlaidError
from findata.security import Keys, load_keys

ACCESS_TOKEN = "access-sandbox-SECRET-123"


def account(account_id: str = "acc_1", current: float = 100.0) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "name": "Plaid Checking",
        "mask": "0000",
        "type": "depository",
        "subtype": "checking",
        "balances": {"current": current, "available": current, "iso_currency_code": "USD"},
    }


def txn(tid: str, amount: float = 12.5, account_id: str = "acc_1", **kw) -> dict[str, Any]:
    base = {
        "transaction_id": tid,
        "account_id": account_id,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": dt.date(2026, 9, 1),
        "authorized_date": None,
        "name": f"Txn {tid}",
        "merchant_name": "Starbucks",
        "pending": False,
        "payment_channel": "in store",
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
        },
    }
    return base | kw


class FakeGateway:
    """In-memory PlaidGateway. `pages` maps cursor -> response (or an exception to raise)."""

    def __init__(self, pages: dict[str | None, Any] | None = None) -> None:
        self.pages = pages or {}
        self.sync_calls: list[str | None] = []
        self.removed_tokens: list[str] = []
        self.remove_error: Exception | None = None

    def create_link_token(self, client_user_id: str) -> str:
        return "link-sandbox-abc"

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        if public_token != "public-sandbox-ok":
            raise PlaidError("INVALID_PUBLIC_TOKEN", "bad token", 400)
        return "item_1", ACCESS_TOKEN

    def sandbox_public_token(self, username: str = "") -> str:
        return "public-sandbox-ok"

    def get_item(self, access_token: str) -> dict[str, Any]:
        assert access_token == ACCESS_TOKEN
        return {
            "item_id": "item_1",
            "institution_id": "ins_109508",
            "institution_name": "First Platypus Bank",
        }

    def item_remove(self, access_token: str) -> None:
        if self.remove_error:
            raise self.remove_error
        self.removed_tokens.append(access_token)

    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500):
        assert access_token == ACCESS_TOKEN
        self.sync_calls.append(cursor)
        page = self.pages[cursor]
        if callable(page):
            page = page()
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        plaid_client_id="test",
        plaid_secret="test",
        findata_data_dir=tmp_path / "data",
        findata_key_backend="file",  # never touch the developer's real OS keychain in tests
        _env_file=None,
    )


@pytest.fixture
def keys(settings) -> Keys:
    return load_keys(settings)


@pytest.fixture
def engine(keys):
    return make_engine(":memory:", keys.db_key)


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def cipher(keys):
    return keys.cipher
