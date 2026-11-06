"""The MCP front end: a daemon client (role `mcp`), read-only unless a human grants writes."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from findata.client import DaemonClient
from findata.client.errors import DaemonNotRunning, DaemonTimeout
from findata.mcp.server import build_mcp_server
from tests.conftest import FakeGateway
from tests.test_daemon_api import _build, _link_and_sync, _pages


@pytest.fixture
def d(settings, keys):
    return _build(settings, keys, FakeGateway(_pages()))


def mcp_client(d) -> DaemonClient:
    http = TestClient(d.client.app, base_url="http://daemon")
    return DaemonClient(d.locations.data_dir, "mcp", http_client=http)


def call(client: DaemonClient, name: str, **arguments):
    """One tool call over an in-memory MCP session; returns the CallToolResult."""

    async def run():
        async with Client(build_mcp_server(client)) as session:
            return await session.call_tool(name, arguments)

    return asyncio.run(run())


def text(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


def test_read_tools_return_daemon_data(d):
    _link_and_sync(d)
    c = mcp_client(d)
    status = call(c, "get_status")
    assert not status.is_error and status.structured_content["plaid_env"]
    items = call(c, "list_items")
    assert [i["id"] for i in items.structured_content["result"]] == ["item_1"]
    accounts = call(c, "list_accounts")
    assert accounts.structured_content["result"][0]["id"] == "acc_1"
    txns = call(c, "list_transactions", account_id="acc_1", limit=1)
    assert len(txns.structured_content["result"]) == 1
    only = call(c, "list_transactions", start="2026-09-02", end="2026-09-02")
    assert [t["id"] for t in only.structured_content["result"]] == ["t1"]
    assert "token" not in (text(status) + text(items) + text(accounts) + text(txns)).lower()


def test_write_tool_without_grant_returns_a_clear_human_must_authorize_error(d):
    _link_and_sync(d)
    r = call(mcp_client(d), "sync")
    assert r.is_error
    assert "WRITE_NOT_GRANTED" in text(r) and "findata grant mcp-write" in text(r)


def test_write_tool_succeeds_after_grant_and_fails_after_expiry_and_revoke(d):
    _link_and_sync(d)
    c = mcp_client(d)
    d.grants.grant({"sync"}, 600)
    ok = call(c, "sync")
    assert not ok.is_error and ok.structured_content["result"][0]["item_id"] == "item_1"
    d.clock.advance(601)
    expired = call(c, "sync")
    assert expired.is_error and "findata grant mcp-write" in text(expired)
    d.grants.grant({"sync"}, 600)
    assert not call(c, "sync").is_error
    d.grants.revoke()
    revoked = call(c, "sync")
    assert revoked.is_error and "WRITE_NOT_GRANTED" in text(revoked)
    assert "findata grant mcp-write" in text(revoked)


def test_sync_grant_does_not_allow_remove_item(d):
    _link_and_sync(d)
    d.grants.grant({"sync"}, 600)
    r = call(mcp_client(d), "remove_item", item_id="item_1")
    assert r.is_error and "WRITE_NOT_GRANTED" in text(r)
    assert "findata grant mcp-write --ops items.remove" in text(r)
    assert [i.id for i in mcp_client(d).items()] == ["item_1"]


def test_remove_item_returns_request_id_then_needs_human_approval(d):
    _link_and_sync(d)
    c = mcp_client(d)
    d.grants.grant({"items.remove"}, 600)
    first = call(c, "remove_item", item_id="item_1")
    assert first.is_error and "CONFIRMATION_REQUIRED" in text(first)
    rid = d.approvals.pending()[0]["request_id"]
    assert rid in text(first) and f"findata approve {rid}" in text(first)
    assert "request_id" in text(first)
    # Still unapproved: retrying with the id does not remove anything.
    assert call(c, "remove_item", item_id="item_1", request_id=rid).is_error
    assert [i.id for i in c.items()] == ["item_1"]
    d.approvals.approve(rid)
    done = call(c, "remove_item", item_id="item_1", request_id=rid)
    assert not done.is_error
    assert c.items() == []


CALLS = [("get_status", {}), ("list_transactions", {}), ("sync", {})]


def test_tools_report_daemon_not_running_clearly(d):
    class Down(DaemonClient):
        def _connect(self):
            raise DaemonNotRunning("x")

    for name, args in CALLS:
        r = call(Down(d.locations.data_dir, "mcp"), name, **args)
        assert r.is_error, name
        assert "daemon is not running" in text(r) and "findata start" in text(r)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_loose_credential_file_is_a_tool_error_naming_the_file(d):
    (d.locations.credentials_dir / "mcp.token").chmod(0o644)
    r = call(mcp_client(d), "get_status")
    assert r.is_error
    assert "mcp.token" in text(r) and "chmod 600" in text(r)


def test_real_missing_socket_reports_not_running(settings, keys):
    from findata.daemon.auth import CredentialStore
    from findata.locations import Locations

    CredentialStore(Locations(settings.data_dir)).ensure_all()  # credentials, but no daemon
    r = call(DaemonClient(settings.data_dir, "mcp"), "get_status")
    assert r.is_error and "findata start" in text(r)


def test_timeout_is_not_reported_as_not_running(d):
    class Slow(DaemonClient):
        def _connect(self):
            raise DaemonTimeout("y")

    for name, args in CALLS:
        r = call(Slow(d.locations.data_dir, "mcp"), name, **args)
        assert r.is_error, name
        assert "did not answer in time" in text(r) and "findata status" in text(r)
        assert "not running" not in text(r) and "findata start" not in text(r)


def test_missing_credential_names_the_problem_and_fix(tmp_path):
    r = call(DaemonClient(tmp_path, "mcp"), "get_status")
    assert r.is_error
    assert "No credential file" in text(r) and "findata init" in text(r)


def test_server_keeps_request_logging_quiet():
    import logging

    build_mcp_server(DaemonClient("/nonexistent", "mcp"))
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING


def test_mcp_module_never_imports_core_modules():
    code = (
        "import sys, findata.mcp, findata.mcp.server, findata.cli\n"
        "core = ('db', 'security', 'plaid_gateway', 'sync', 'linking', 'waiting', 'queries',\n"
        "        'schema', 'runtime', 'contexts', 'config')\n"
        "print([m for m in core if f'findata.{m}' in sys.modules])\n"
    )
    out = subprocess.run(  # noqa: S603 (our own interpreter and a fixed script)
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_tool_list_marks_write_tools_as_requiring_a_grant(d):
    async def run():
        async with Client(build_mcp_server(mcp_client(d))) as session:
            return (await session.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {
        "get_status",
        "list_items",
        "list_accounts",
        "list_transactions",
        "sync",
        "remove_item",
    }
    for name in ("get_status", "list_items", "list_accounts", "list_transactions"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["sync"].annotations.read_only_hint is False
    assert tools["remove_item"].annotations.destructive_hint is True
    assert "grant" in tools["sync"].description and "`sync`" in tools["sync"].description
    desc = tools["remove_item"].description
    assert "`items.remove`" in desc and "approval" in desc and "grant" in desc
