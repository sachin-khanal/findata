"""The CLI's data commands as clients of the daemon, and the human controls (grant, approve)."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys

import pytest

from findata.client import DaemonClient
from findata.daemon.auth import CredentialStore
from findata.locations import Locations
from tests.conftest import FakeGateway
from tests.test_daemon_api import _build, _link_and_sync, _pages
from tests.test_daemon_presence import DENIED, _agent_request
from tests.test_wait_and_cli import Clock, slow_history_gateway

NOTICE = "Confirm on your Mac (Touch ID or password)…"


@pytest.fixture
def d(settings, keys):
    return _build(settings, keys, FakeGateway(_pages()))


@pytest.fixture
def invoke(cli_against, d):
    return cli_against(d.client.app)


@pytest.fixture
def mac(monkeypatch):
    """The presence notice is printed only on macOS, the one platform with a presence check."""
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_presence_notice_is_mac_only(platform, invoke, d, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    r = invoke("grant", "mcp-write", "--json")
    assert r.exit_code == 0, r.output
    assert NOTICE not in r.output and "Touch ID" not in r.stderr


def _last(result) -> dict:
    return json.loads(result.stdout.splitlines()[-1])


# ---------- no daemon ----------


@pytest.mark.parametrize(
    "args",
    [
        ["sync"],
        ["txns"],
        ["items", "list"],
        ["items", "rm", "x"],
        ["sandbox-link"],
        ["audit"],
        ["status"],
        ["revoke", "mcp-write"],
        ["grant", "mcp-write"],
        ["approve", "r1"],
    ],
)
def test_every_data_command_reports_daemon_not_running(args, settings, monkeypatch):
    monkeypatch.setenv("FINDATA_DATA_DIR", str(settings.data_dir))
    CredentialStore(Locations(settings.data_dir)).ensure_all()  # `findata init` ran; no daemon
    from typer.testing import CliRunner

    from findata import cli

    r = CliRunner().invoke(cli.app, [*args, "--json"])
    assert r.exit_code == 1
    assert _last(r) == {
        "event": "error",
        "message": "The findata daemon is not running. Start it with `findata start`.",
        "error_code": "DAEMON_NOT_RUNNING",
    }
    assert "Traceback" not in r.output and (
        r.exception is None or isinstance(r.exception, SystemExit)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_loose_credential_file_is_one_json_error_event_not_a_traceback(invoke, d):
    (d.locations.credentials_dir / "cli.token").chmod(0o644)
    r = invoke("status", "--json")
    assert r.exit_code == 1
    event = _last(r)
    assert event["event"] == "error" and "chmod 600" in event["message"]
    assert "cli.token" in event["message"]
    assert len(r.stdout.splitlines()) == 1 and "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_stop_without_a_daemon_says_so(settings, monkeypatch):
    monkeypatch.setenv("FINDATA_DATA_DIR", str(settings.data_dir))
    CredentialStore(Locations(settings.data_dir)).ensure_all()
    from typer.testing import CliRunner

    from findata import cli

    r = CliRunner().invoke(cli.app, ["stop"])
    assert r.exit_code == 1 and "daemon is not running" in r.output


# ---------- data commands ----------


def test_data_commands_read_through_the_daemon(invoke, d):
    _link_and_sync(d)
    t = invoke("txns", "--json", "--limit", "5")
    rows = [json.loads(line) for line in t.stdout.splitlines()]
    assert {r["id"] for r in rows} == {"t1", "t2"} and rows[0]["category"] == "FOOD_AND_DRINK"
    assert "Starbucks" in invoke("txns").output
    s = invoke("status", "--json")
    assert s.exit_code == 0 and _last(s)["user_presence"] == "touch_id"
    assert "user presence" in invoke("status").output
    a = invoke("audit", "--json", "--limit", "50")
    assert {json.loads(line)["operation"] for line in a.stdout.splitlines()} >= {"sync"}
    assert invoke("stop").exit_code == 0 and d.shutdowns == [True]


def test_items_rm_matches_a_unique_prefix_and_refuses_ambiguity(invoke, d, mac):
    _link_and_sync(d)
    miss = invoke("items", "rm", "zzz", "--yes", "--json")
    assert miss.exit_code == 1 and _last(miss)["error_code"] == "NO_UNIQUE_MATCH"
    r = invoke("items", "rm", "item_", "--yes", "--json")
    assert r.exit_code == 0 and _last(r) == {"event": "removed", "item_id": "item_"}
    assert d.presence.reasons == ["remove First Platypus Bank and its local data"]
    assert NOTICE in r.stderr and NOTICE not in r.stdout
    assert "No linked items" in invoke("items", "list").output


# ---------- grants, approvals ----------


def test_grant_and_revoke_round_trip(invoke, d, mac):
    r = invoke("grant", "mcp-write", "--json")  # least privilege: sync only, 15 minutes
    assert r.exit_code == 0, r.output
    assert NOTICE in r.stderr and NOTICE not in r.stdout
    assert _last(r)["event"] == "granted" and _last(r)["operations"] == ["sync"]
    grant = d.grants.current()
    assert grant.operations == ["sync"]
    assert (grant.expires_at - d.clock()).total_seconds() == 15 * 60
    assert d.presence.reasons == ["allow the findata agent to sync for 15 minutes"]

    r = invoke("grant", "mcp-write", "--ops", "sync, items.remove", "--for", "2h")
    assert r.exit_code == 0 and "items.remove, sync" in r.stdout
    assert d.grants.current().operations == ["items.remove", "sync"]
    assert (d.grants.current().expires_at - d.clock()).total_seconds() == 2 * 3600

    r = invoke("revoke", "mcp-write", "--json")
    assert r.exit_code == 0 and _last(r) == {"event": "revoked"}
    assert d.grants.current() is None
    assert len(d.presence.reasons) == 2  # revoking never asks


@pytest.mark.parametrize(
    "args",
    [
        ["grant", "everything"],
        ["grant", "mcp-write", "--for", "25h"],
        ["grant", "mcp-write", "--for", "86401s"],
        ["grant", "mcp-write", "--for", "15"],
        ["grant", "mcp-write", "--for", "0m"],
        ["grant", "mcp-write", "--ops", "delete-everything"],
        ["revoke", "agent"],
    ],
)
def test_bad_grant_requests_fail_clearly_without_asking(args, invoke, d):
    r = invoke(*args, "--json")
    assert r.exit_code == 1 and _last(r)["event"] == "error" and _last(r)["error_code"]
    assert d.grants.current() is None and d.presence.reasons == []


def test_grant_accepts_up_to_24_hours_in_any_unit(invoke, d):
    for text, seconds in (("24h", 86400), ("1440m", 86400), ("90s", 90)):
        assert invoke("grant", "mcp-write", "--for", text).exit_code == 0
        assert (d.grants.current().expires_at - d.clock()).total_seconds() == seconds


def test_approve_unblocks_an_agent_removal(invoke, d, mac):
    _link_and_sync(d)
    rid = _agent_request(d)
    r = invoke("approve", rid, "--json")
    assert r.exit_code == 0 and _last(r) == {"event": "approved", "request_id": rid}
    assert NOTICE in r.stderr
    assert (
        d.client.delete(f"/v1/items/item_1?request_id={rid}", headers=d.as_["mcp"]).status_code
        == 204
    )
    # the request is used up: approving it again says it is gone
    again = invoke("approve", rid, "--json")
    assert again.exit_code == 1 and _last(again)["error_code"] == "NOT_FOUND"
    assert "expired" in _last(again)["message"] and "approved already" in _last(again)["message"]


def test_cli_approve_accepts_every_request_id_shape(invoke, d, monkeypatch):
    _link_and_sync(d)
    # the random source returns a dash-first token; `approve` must still take the id it was given
    monkeypatch.setattr(secrets, "token_urlsafe", lambda nbytes: "-" + "x" * 10)
    rid = _agent_request(d)
    r = invoke("approve", rid, "--json")
    assert r.exit_code == 0 and _last(r) == {"event": "approved", "request_id": rid}


@pytest.mark.parametrize("command", ["grant", "approve", "items rm"])
def test_presence_refusal_is_reported_clearly(command, invoke, d, mac):
    _link_and_sync(d)
    rid = _agent_request(d)
    d.presence.outcome = "denied"
    args = {
        "grant": ["grant", "mcp-write"],
        "approve": ["approve", rid],
        "items rm": ["items", "rm", "item_1", "--yes"],
    }[command]
    r = invoke(*args, "--json")
    assert r.exit_code == 1
    assert _last(r) == {"event": "error", "message": DENIED, "error_code": "USER_PRESENCE_REQUIRED"}
    assert NOTICE in r.stderr  # it did ask; the answer was no
    assert [i["id"] for i in d.client.get("/v1/items", headers=d.as_["cli"]).json()] == ["item_1"]
    assert [p["request_id"] for p in d.approvals.pending()] == [rid]  # still pending, not approved
    # a grant from `_agent_request` exists; only the CLI's own grant must be absent
    assert d.grants.current().operations == ["items.remove"]


# ---------- sandbox-link ----------


def test_sandbox_link_follows_the_job_stream_without_polling(
    cli_against, settings, keys, monkeypatch
):
    from tests.test_daemon_jobs import _build as build_with_clock

    clock = Clock()
    jobs = build_with_clock(settings, keys, slow_history_gateway(), clock.sleep, clock.monotonic)
    invoke = cli_against(jobs.app)
    calls = []
    real_follow = DaemonClient.follow_job

    def follow(self, job_id):
        calls.append(job_id)
        return real_follow(self, job_id)

    def never(self, *a, **kw):
        raise AssertionError("the CLI re-read a job instead of following its stream")

    monkeypatch.setattr(DaemonClient, "follow_job", follow)
    monkeypatch.setattr(DaemonClient, "job", never)
    r = invoke("sandbox-link", "--json", "--interval", "5")
    assert r.exit_code == 0, r.output
    assert len(calls) == 1
    assert [json.loads(line)["event"] for line in r.stdout.splitlines()][-1] == "done"


def test_importing_the_cli_loads_no_core_modules():
    """Ruling R7: front ends never load the key, database or Plaid code just by importing."""
    code = (
        "import sys, findata.cli\n"
        "core = ('db', 'security', 'plaid_gateway', 'sync', 'linking', 'waiting', 'queries',\n"
        "        'schema', 'runtime', 'contexts', 'config')\n"
        "print([m for m in core if f'findata.{m}' in sys.modules])\n"
    )
    out = subprocess.run(  # noqa: S603 (our own interpreter and a fixed script)
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
