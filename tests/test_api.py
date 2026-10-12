import pytest
from fastapi.testclient import TestClient

from findata.api import TOKEN_HEADER, create_app
from findata.security import load_or_create_api_token
from tests.conftest import FakeGateway, account, txn


@pytest.fixture
def gateway():
    return FakeGateway(
        {
            None: {
                "added": [txn("t1", date="2026-09-02"), txn("t2")],
                "modified": [],
                "removed": [],
                "accounts": [account()],
                "next_cursor": "c1",
                "has_more": False,
                "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            }
        }
    )


@pytest.fixture
def client(settings, gateway, engine):
    app = create_app(settings, gateway=gateway, engine=engine)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def auth(settings):
    return {TOKEN_HEADER: load_or_create_api_token(settings.api_token_path)}


def test_health_is_public(client):
    assert client.get("/api/health").json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/api/accounts", "/api/transactions", "/api/items"])
def test_api_requires_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={TOKEN_HEADER: "wrong"}).status_code == 401


def test_rejects_foreign_host_header(settings, gateway, engine):
    c = TestClient(
        create_app(settings, gateway=gateway, engine=engine), base_url="http://evil.test"
    )
    assert c.get("/api/health").status_code == 400


def test_index_injects_token(client, auth):
    html = client.get("/").text
    assert auth[TOKEN_HEADER] in html and "__FINDATA_TOKEN__" not in html


def test_link_exchange_sync_and_query(client, auth):
    assert client.post("/api/link/token", headers=auth).json()["link_token"].startswith("link-")
    item = client.post(
        "/api/link/exchange", headers=auth, json={"public_token": "public-sandbox-ok"}
    )
    assert item.status_code == 200 and item.json()["institution_name"] == "First Platypus Bank"

    sync = client.post("/api/sync", headers=auth).json()
    assert sync == [
        {
            "item_id": "item_1",
            "added": 2,
            "modified": 0,
            "removed": 0,
            "status": "HISTORICAL_UPDATE_COMPLETE",
        }
    ]

    accts = client.get("/api/accounts", headers=auth).json()
    assert accts[0]["current_balance"] == "100.0"  # money stays an exact decimal string

    txns = client.get("/api/transactions", headers=auth).json()
    assert [t["id"] for t in txns] == ["t1", "t2"]  # newest first
    only = client.get("/api/transactions", headers=auth, params={"start": "2026-09-02"}).json()
    assert [t["id"] for t in only] == ["t1"]


def test_plaid_errors_surface_as_502(client, auth):
    r = client.post("/api/link/exchange", headers=auth, json={"public_token": "bad"})
    assert r.status_code == 502 and r.json()["error_code"] == "INVALID_PUBLIC_TOKEN"
