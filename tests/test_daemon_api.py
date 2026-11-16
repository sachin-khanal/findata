"""The daemon's /v1 API: roles, grants, approvals, audit, serialized sync, error shapes."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import findata
from findata.daemon.app import create_daemon_app
from findata.daemon.audit import AuditLog
from findata.daemon.auth import CredentialStore
from findata.daemon.grants import GRANTABLE, ApprovalStore, GrantStore
from findata.db import make_engine, make_session_factory
from findata.locations import Locations
from findata.plaid_gateway import PlaidError
from findata.runtime import Runtime
from findata.schema import ensure_current, head_revision
from tests.conftest import ACCESS_TOKEN, FakeGateway, FakePresence, account, txn

CRED = "X-Findata-Credential"
ROLES = ("web", "cli", "mcp")


class FakeClock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 21, 9, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


def _page(cursor: str, added: list) -> dict:
    return {
        "added": added,
        "modified": [],
        "removed": [],
        "accounts": [account()],
        "next_cursor": cursor,
        "has_more": False,
        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
    }


def _pages() -> dict:
    return {
        None: _page("c1", [txn("t1", date=dt.date(2026, 9, 2)), txn("t2", amount=987.65)]),
        "c1": _page("c1", []),
    }


class OverlapGateway(FakeGateway):
    """Records how many transactions_sync calls are in flight at once."""

    def __init__(self, pages) -> None:
        super().__init__(pages)
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def transactions_sync(self, access_token, cursor, count=500):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.2)  # long enough that an unserialized second call would overlap
            return super().transactions_sync(access_token, cursor, count)
        finally:
            with self._guard:
                self.active -= 1


@pytest.fixture
def gateway():
    return OverlapGateway(_pages())


def _build(settings, keys, gateway, *, audit_cls=AuditLog, presence=None):
    clock = FakeClock()
    presence = FakePresence("confirmed") if presence is None else presence
    # A file database (not :memory:/StaticPool) so concurrent requests get their own connection.
    engine = make_engine(settings.db_path, keys.db_key)
    ensure_current(engine)
    runtime = Runtime(settings, make_session_factory(engine), keys.cipher, gateway)
    locations = Locations(settings.data_dir)
    credentials = CredentialStore(locations)
    credentials.ensure_all()
    grants, approvals = GrantStore(clock), ApprovalStore(clock)
    audit = audit_cls(runtime.session_factory, clock)
    shutdowns: list[bool] = []
    app = create_daemon_app(
        runtime,
        credentials,
        grants,
        approvals,
        audit,
        started_at=clock(),
        clock=clock,
        presence=presence,
        on_shutdown=lambda: shutdowns.append(True),
    )
    tokens = {r: (locations.credentials_dir / f"{r}.token").read_text().strip() for r in ROLES}
    return SimpleNamespace(
        client=TestClient(app, base_url="http://127.0.0.1"),
        as_={r: {CRED: t} for r, t in tokens.items()},
        tokens=tokens,
        clock=clock,
        grants=grants,
        approvals=approvals,
        gateway=gateway,
        shutdowns=shutdowns,
        locations=locations,
        presence=presence,
    )


@pytest.fixture
def d(settings, keys, gateway):
    return _build(settings, keys, gateway)


def _link_and_sync(d) -> None:
    r = d.client.post(
        "/v1/link/exchange", headers=d.as_["cli"], json={"public_token": "public-sandbox-ok"}
    )
    assert r.status_code == 200, r.text
    assert d.client.post("/v1/sync", headers=d.as_["cli"]).status_code == 200


def _error(r, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error_code"] == code and isinstance(body["detail"], str) and body["detail"]
    return body


def _audit(d, limit: int = 100) -> list[dict]:
    r = d.client.get("/v1/audit", headers=d.as_["cli"], params={"limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- authentication ----------


def test_no_token_is_401_and_unknown_token_is_401(d):
    for headers in ({}, {CRED: ""}, {CRED: "not-a-real-credential"}, {CRED: "é".encode()}):
        _error(d.client.get("/v1/items", headers=headers), 401, "UNAUTHENTICATED")
    # a credential from another install does not work here either
    other = CredentialStore(Locations(d.locations.data_dir.parent / "other"))
    other.ensure_all()
    foreign = (d.locations.data_dir.parent / "other/credentials/web.token").read_text().strip()
    _error(d.client.get("/v1/items", headers={CRED: foreign}), 401, "UNAUTHENTICATED")


def test_unencodable_credential_is_401_not_500(d, monkeypatch):
    def boom(self, token):
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(CredentialStore, "role_for", boom)
    _error(d.client.get("/v1/items", headers={CRED: "x"}), 401, "UNAUTHENTICATED")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_broken_credential_file_is_500_without_secrets(d):
    (d.locations.credentials_dir / "mcp.token").chmod(0o644)
    r = d.client.get("/v1/items", headers=d.as_["cli"])
    body = _error(r, 500, "CREDENTIALS_UNAVAILABLE")
    assert "chmod 600" in body["detail"]
    assert not any(t in r.text for t in d.tokens.values())
    (d.locations.credentials_dir / "mcp.token").chmod(0o600)
    rows = _audit(d)
    assert rows[0]["role"] == "anonymous" and rows[0]["outcome"] == "error"
    assert not any(t in json.dumps(rows) for t in d.tokens.values())


def test_unauthenticated_callers_get_401_even_for_bad_bodies_and_unknown_routes(d):
    r = d.client.post(
        "/v1/link/exchange", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _error(r, 401, "UNAUTHENTICATED")
    _error(d.client.get("/v1/transactions", params={"limit": 0}), 401, "UNAUTHENTICATED")
    _error(d.client.get("/v1/no-such-route"), 401, "UNAUTHENTICATED")
    _error(d.client.get("/openapi.json"), 401, "UNAUTHENTICATED")  # no unauthenticated docs


def test_validation_errors_and_unknown_routes_use_the_error_shape(d):
    cli = d.as_["cli"]
    for limit in (0, 501):
        r = d.client.get("/v1/transactions", headers=cli, params={"limit": limit})
        _error(r, 422, "VALIDATION_ERROR")
    _error(d.client.get("/v1/no-such-route", headers=cli), 404, "NOT_FOUND")
    _error(d.client.delete("/v1/items/nope", headers=cli), 404, "NOT_FOUND")
    r = d.client.post("/v1/link/exchange", headers=cli, json={"wrong": "field"})
    _error(r, 422, "VALIDATION_ERROR")


# ---------- reads ----------


def test_read_routes_work_for_every_role(d):
    _link_and_sync(d)
    for role in ROLES:
        h = d.as_[role]
        assert d.client.get("/v1/status", headers=h).status_code == 200
        items = d.client.get("/v1/items", headers=h).json()
        assert items == [
            {
                "id": "item_1",
                "institution_name": "First Platypus Bank",
                "last_synced_at": items[0]["last_synced_at"],
                "accounts": 1,
                "transactions": 2,
            }
        ]
        accts = d.client.get("/v1/accounts", headers=h).json()
        assert accts[0]["current_balance"] == "100.0"
        txns = d.client.get("/v1/transactions", headers=h).json()
        assert [t["id"] for t in txns] == ["t1", "t2"]  # newest first


def test_transactions_keep_the_current_filters(d):
    _link_and_sync(d)
    get = lambda **p: [  # noqa: E731
        t["id"] for t in d.client.get("/v1/transactions", headers=d.as_["mcp"], params=p).json()
    ]
    assert get(start="2026-09-02") == ["t1"]
    assert get(end="2026-09-01") == ["t2"]
    assert get(account_id="acc_1", limit=1) == ["t1"]
    assert get(limit=1, offset=1) == ["t2"]
    assert get(category="FOOD_AND_DRINK") == ["t1", "t2"]
    assert get(category="TRAVEL") == []


def test_status_reports_version_schema_and_uptime(d):
    d.clock.advance(30)
    s = d.client.get("/v1/status", headers=d.as_["mcp"]).json()
    assert s["version"] == findata.__version__
    assert s["plaid_env"] == "sandbox"
    assert s["schema_revision"] == head_revision()
    assert s["uptime_s"] == 30.0
    assert dt.datetime.fromisoformat(s["started_at"]) == d.clock.now - dt.timedelta(seconds=30)


# ---------- writes, grants and approvals ----------


def test_mcp_cannot_sync_without_grant_and_error_names_the_fix(d):
    for method, path in (
        ("POST", "/v1/sync"),
        ("POST", "/v1/link/token"),
        ("DELETE", "/v1/items/item_1"),
    ):
        body = _error(
            d.client.request(method, path, headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED"
        )
        assert "findata grant mcp-write" in body["detail"]
    assert d.gateway.sync_calls == []


def test_mcp_can_sync_after_grant_then_not_after_expiry(d):
    _link_and_sync(d)
    r = d.client.post(
        "/v1/grants", headers=d.as_["cli"], json={"operations": ["sync"], "ttl_s": 60}
    )
    assert r.status_code == 200 and r.json()["operations"] == ["sync"]
    assert d.client.post("/v1/sync", headers=d.as_["mcp"]).status_code == 200
    d.clock.advance(60)
    _error(d.client.post("/v1/sync", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")
    # a revoked grant stops working at once too
    d.client.post("/v1/grants", headers=d.as_["web"], json={"operations": ["sync"], "ttl_s": 60})
    assert d.client.post("/v1/sync", headers=d.as_["mcp"]).status_code == 200
    assert d.client.delete("/v1/grants", headers=d.as_["web"]).status_code == 204
    _error(d.client.post("/v1/sync", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")


def test_grant_for_sync_does_not_allow_remove(d):
    _link_and_sync(d)
    d.client.post("/v1/grants", headers=d.as_["cli"], json={"operations": ["sync"], "ttl_s": 600})
    _error(d.client.delete("/v1/items/item_1", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")
    _error(d.client.post("/v1/link/token", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")
    assert d.approvals.pending() == []  # refused before any approval request is made


def test_mcp_cannot_use_admin_routes_even_with_a_grant(d):
    d.grants.grant(set(GRANTABLE), 600)
    before = d.grants.current()
    mcp = d.as_["mcp"]
    for method, path, kw in (
        ("POST", "/v1/grants", {"json": {"operations": ["sync"], "ttl_s": 86400}}),
        ("GET", "/v1/grants", {}),
        ("DELETE", "/v1/grants", {}),
        ("GET", "/v1/approvals", {}),
        ("POST", "/v1/approvals/abc", {}),
        ("GET", "/v1/audit", {}),
        ("POST", "/v1/shutdown", {}),
    ):
        _error(d.client.request(method, path, headers=mcp, **kw), 403, "FORBIDDEN")
    assert d.grants.current() == before  # it could neither widen nor drop its own grant
    assert d.shutdowns == []


def test_grants_round_trip_and_validation(d):
    cli = d.as_["cli"]
    assert d.client.get("/v1/grants", headers=cli).json() is None
    r = d.client.post(
        "/v1/grants", headers=cli, json={"operations": ["sync", "items.remove"], "ttl_s": 900}
    )
    assert r.status_code == 200
    assert d.client.get("/v1/grants", headers=cli).json() == r.json()
    assert r.json()["operations"] == ["items.remove", "sync"]
    for bad in (
        {"operations": [], "ttl_s": 60},
        {"operations": ["admin"], "ttl_s": 60},
        {"operations": ["sync", "db.drop"], "ttl_s": 60},
        {"operations": ["sync"], "ttl_s": 0},
        {"operations": ["sync"], "ttl_s": 86401},
    ):
        _error(d.client.post("/v1/grants", headers=cli, json=bad), 422, "VALIDATION_ERROR")
    assert d.client.get("/v1/grants", headers=cli).json() == r.json()  # bad ones changed nothing
    assert d.client.delete("/v1/grants", headers=cli).status_code == 204
    assert d.client.get("/v1/grants", headers=cli).json() is None


def test_remove_by_agent_needs_a_human_approval_once(d):
    _link_and_sync(d)
    d.client.post(
        "/v1/grants", headers=d.as_["cli"], json={"operations": ["items.remove"], "ttl_s": 600}
    )
    mcp = d.as_["mcp"]
    body = _error(d.client.delete("/v1/items/item_1", headers=mcp), 403, "CONFIRMATION_REQUIRED")
    rid = body["request_id"]
    assert rid in body["detail"]
    # an unapproved id does not work, and the agent cannot approve its own request
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert _error(r, 403, "CONFIRMATION_REQUIRED")["request_id"] == rid
    _error(d.client.post(f"/v1/approvals/{rid}", headers=mcp), 403, "FORBIDDEN")
    assert len(d.client.get("/v1/items", headers=mcp).json()) == 1

    pending = d.client.get("/v1/approvals", headers=d.as_["cli"]).json()
    assert [(p["request_id"], p["operation"], p["subject"]) for p in pending] == [
        (rid, "items.remove", "item_1")
    ]
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["cli"]).status_code == 204
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert r.status_code == 204, r.text
    assert d.gateway.removed_tokens == [ACCESS_TOKEN]
    assert d.client.get("/v1/items", headers=mcp).json() == []

    # the same approval cannot be used twice: re-link the item and retry with the old id
    _link_and_sync(d)
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert _error(r, 403, "CONFIRMATION_REQUIRED")["request_id"] != rid
    assert len(d.client.get("/v1/items", headers=mcp).json()) == 1


def test_agent_retries_reuse_one_pending_request(d):
    _link_and_sync(d)
    d.grants.grant({"items.remove"}, 600)
    ids = {
        d.client.delete("/v1/items/item_1", headers=d.as_["mcp"]).json()["request_id"]
        for _ in range(3)
    }
    assert len(ids) == 1 and len(d.approvals.pending()) == 1


def test_agent_cannot_remove_local_only_even_with_an_approval(d):
    _link_and_sync(d)
    d.grants.grant(set(GRANTABLE), 600)
    mcp = d.as_["mcp"]
    local = {"local_only": True}
    # refused before anything happens: no pending request is created for it
    body = _error(d.client.delete("/v1/items/item_1", headers=mcp, params=local), 403, "FORBIDDEN")
    assert "local" in body["detail"].lower() and "human" in body["detail"].lower()
    assert d.approvals.pending() == []

    rid = d.client.delete("/v1/items/item_1", headers=mcp).json()["request_id"]
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["cli"]).status_code == 204
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={**local, "request_id": rid})
    _error(r, 403, "FORBIDDEN")
    assert len(d.client.get("/v1/items", headers=mcp).json()) == 1  # item untouched
    assert d.gateway.removed_tokens == []

    # the approval was not used up: the removal the human approved still works
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert r.status_code == 204, r.text
    assert d.gateway.removed_tokens == [ACCESS_TOKEN]  # revoked at Plaid, not orphaned


def test_approving_an_unknown_request_is_404(d):
    _error(d.client.post("/v1/approvals/nope", headers=d.as_["cli"]), 404, "NOT_FOUND")


@pytest.mark.parametrize("role", ["cli", "web"])
def test_humans_remove_items_without_an_approval(d, role):
    _link_and_sync(d)
    r = d.client.delete("/v1/items/item_1", headers=d.as_[role], params={"local_only": True})
    assert r.status_code == 204, r.text
    assert d.gateway.removed_tokens == []  # local_only skipped Plaid
    assert d.client.get("/v1/items", headers=d.as_[role]).json() == []
    assert d.approvals.pending() == []


def test_link_token_and_exchange(d):
    r = d.client.post("/v1/link/token", headers=d.as_["web"])
    assert r.json() == {"link_token": "link-sandbox-abc"}
    r = d.client.post(
        "/v1/link/exchange", headers=d.as_["web"], json={"public_token": "public-sandbox-ok"}
    )
    assert r.json()["id"] == "item_1" and r.json()["institution_name"] == "First Platypus Bank"


def test_shutdown_calls_back_for_humans(d):
    assert d.client.post("/v1/shutdown", headers=d.as_["cli"]).status_code == 202
    assert d.shutdowns == [True]


# ---------- Plaid ----------


def test_plaid_error_is_502_with_plaid_code(d):
    r = d.client.post("/v1/link/exchange", headers=d.as_["cli"], json={"public_token": "bad"})
    _error(r, 502, "INVALID_PUBLIC_TOKEN")
    _link_and_sync(d)
    d.gateway.pages["c1"] = PlaidError("ITEM_LOGIN_REQUIRED", "login expired", 400)
    _error(d.client.post("/v1/sync", headers=d.as_["cli"]), 502, "ITEM_LOGIN_REQUIRED")


def test_unexpected_error_is_500_without_its_message_and_is_audited(d, caplog):
    _link_and_sync(d)
    planted = "boom near Starbucks 987.65"
    d.gateway.pages["c1"] = ValueError(planted)
    # d.client re-raises anything the app lets escape; a server would log that in full, so the
    # daemon must handle it itself rather than pass the exception (and its message) on.
    with caplog.at_level(logging.DEBUG):
        r = d.client.post("/v1/sync", headers=d.as_["cli"])
    _error(r, 500, "INTERNAL_ERROR")
    assert "Starbucks" not in r.text and "987.65" not in r.text
    assert any("ValueError" in rec.getMessage() for rec in caplog.records)  # the type is logged
    for text in [caplog.text, *(rec.getMessage() for rec in caplog.records)]:
        assert "Starbucks" not in text and "987.65" not in text and planted not in text
    assert not any(rec.exc_info for rec in caplog.records)  # no traceback with the message
    newest = _audit(d)[0]
    assert (newest["role"], newest["operation"], newest["outcome"]) == ("cli", "sync", "error")


def test_missing_plaid_keys_is_503(settings, keys):
    settings.plaid_client_id = ""
    d = _build(settings, keys, None)
    _error(d.client.post("/v1/sync", headers=d.as_["cli"]), 503, "NOT_CONFIGURED")
    assert d.client.get("/v1/items", headers=d.as_["cli"]).status_code == 200  # reads still work


def test_concurrent_syncs_are_serialized(d):
    _link_and_sync(d)
    d.gateway.sync_calls.clear()
    start = threading.Barrier(2)
    statuses: list[int] = []

    def call():
        start.wait()
        statuses.append(d.client.post("/v1/sync", headers=d.as_["cli"]).status_code)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert statuses == [200, 200]
    assert d.gateway.max_active == 1  # never two syncs talking to Plaid at once
    assert d.gateway.sync_calls == ["c1", "c1"]


# ---------- audit ----------


def test_every_request_is_audited_without_data(d):
    _link_and_sync(d)
    d.client.post("/v1/link/token", headers=d.as_["web"])
    d.client.get(
        "/v1/transactions",
        headers=d.as_["mcp"],
        params={"category": "FOOD_AND_DRINK", "start": "2026-09-01", "Starbucks987.65": "x"},
    )
    d.client.get("/v1/items", headers=d.as_["web"])
    d.client.delete("/v1/items/item_1", headers=d.as_["cli"])

    rows = _audit(d)
    assert [(r["role"], r["operation"], r["outcome"]) for r in rows] == [
        ("cli", "items.remove", "ok"),
        ("web", "items.list", "ok"),
        ("mcp", "transactions.list", "ok"),
        ("web", "link.token", "ok"),
        ("cli", "sync", "ok"),
        ("cli", "link.exchange", "ok"),
    ]  # newest first
    for r in rows:
        assert set(r) == {"id", "at", "role", "operation", "summary", "outcome"}
    by_op = {r["operation"]: r["summary"] for r in rows}
    assert by_op["items.remove"] == "DELETE /v1/items/{item_id} presence=confirmed"
    assert by_op["transactions.list"].startswith("GET /v1/transactions")
    assert "category" in by_op["transactions.list"] and "start" in by_op["transactions.list"]

    dumped = json.dumps(rows)
    forbidden = [
        "Txn t1",
        "Txn t2",
        "Starbucks",
        "12.5",
        "987.65",
        "FOOD_AND_DRINK",
        "2026-09-01",
        "item_1",
        "First Platypus",
        ACCESS_TOKEN,
        "public-sandbox-ok",
        "link-sandbox-abc",
        *d.tokens.values(),
    ]
    assert [f for f in forbidden if f in dumped] == []


def test_denied_requests_are_audited_too(d):
    d.client.get("/v1/items")
    d.client.get("/v1/items", headers={CRED: "wrong"})
    d.client.post("/v1/sync", headers=d.as_["mcp"])
    d.client.get("/v1/audit", headers=d.as_["mcp"])
    d.client.get("/v1/no-such-route", headers=d.as_["cli"])
    d.client.get("/v1/transactions", headers=d.as_["cli"], params={"limit": 0})
    rows = _audit(d)
    assert [(r["role"], r["operation"], r["outcome"]) for r in rows] == [
        ("cli", "transactions.list", "error"),
        ("cli", "unknown", "error"),  # an unknown route is still attributed to its caller
        ("mcp", "audit.list", "denied"),
        ("mcp", "sync", "denied"),
        ("anonymous", "items.list", "denied"),
        ("anonymous", "items.list", "denied"),
    ]
    assert "wrong" not in json.dumps(rows) and "no-such-route" not in json.dumps(rows)


def test_audit_limit_is_bounded(d):
    for _ in range(3):
        d.client.get("/v1/items", headers=d.as_["cli"])
    assert len(_audit(d, limit=2)) == 2
    _error(
        d.client.get("/v1/audit", headers=d.as_["cli"], params={"limit": 0}),
        422,
        "VALIDATION_ERROR",
    )


class BrokenAudit(AuditLog):
    def record(self, role, operation, summary, outcome):
        raise RuntimeError("disk full while writing secret-ish stuff")


def test_audit_failure_never_fails_the_request(settings, keys, gateway, caplog):
    d = _build(settings, keys, gateway, audit_cls=BrokenAudit)
    with caplog.at_level(logging.WARNING):
        r = d.client.get("/v1/transactions", headers=d.as_["cli"], params={"category": "X1"})
    assert r.status_code == 200
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings and "audit" in warnings[0].getMessage().lower()
    text = caplog.text
    assert "secret-ish" not in text and "X1" not in text and d.tokens["cli"] not in text


# ---------- structural: every /v1 route authenticates ----------

# Routes that may be called without a role credential. None today: add one here only on purpose.
OPEN_V1_ROUTES: set[str] = set()


def _calls(dependant) -> set:
    """Every callable in a route's dependency tree (router-level dependencies included)."""
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def test_every_v1_route_depends_on_resolve_role(d):
    from findata.daemon.deps import resolve_role
    from tests.test_web_session import _api_routes

    routes = [r for r in _api_routes(d.client.app.routes) if r.path.startswith("/v1")]
    assert len(routes) >= 15 and any(r.path == "/v1/items/{item_id}" for r in routes)
    unprotected = sorted(
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if r.path not in OPEN_V1_ROUTES and resolve_role not in _calls(r.dependant)
    )
    assert unprotected == []


# ---------- revoke while an agent write is in flight ----------


class GatedGateway(FakeGateway):
    """Once armed, transactions_sync signals `entered` and waits for `release`."""

    def __init__(self, pages) -> None:
        super().__init__(pages)
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def transactions_sync(self, access_token, cursor, count=500):
        if self.armed:
            self.entered.set()
            assert self.release.wait(10), "the test never released the gateway"
        return super().transactions_sync(access_token, cursor, count)


def test_revoke_during_an_agent_sync_lets_it_finish_and_refuses_the_next(settings, keys):
    gateway = GatedGateway(_pages())
    d = _build(settings, keys, gateway)
    _link_and_sync(d)
    d.grants.grant({"sync"}, 600)
    gateway.armed = True
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(r=d.client.post("/v1/sync", headers=d.as_["mcp"]))
    )
    worker.start()
    try:
        assert gateway.entered.wait(10), "the agent's sync never reached Plaid"
        # The grant is checked once, at admission: revoking now does not stop this sync.
        assert d.client.delete("/v1/grants", headers=d.as_["cli"]).status_code == 204
    finally:
        gateway.release.set()
        worker.join(10)
    assert result["r"].status_code == 200, result["r"].text
    gateway.armed = False
    _error(d.client.post("/v1/sync", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")
    rows = [(r["role"], r["operation"], r["outcome"]) for r in _audit(d)]
    assert rows[:3] == [
        ("mcp", "sync", "denied"),
        ("mcp", "sync", "ok"),
        ("cli", "grants.revoke", "ok"),
    ]
