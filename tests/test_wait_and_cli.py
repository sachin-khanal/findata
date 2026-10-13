"""Waiting for Plaid history to load, and the explicit CLI / API messaging around it."""

import json

import pytest
from typer.testing import CliRunner

from findata import cli
from findata.config import Settings
from findata.db import make_session_factory
from findata.plaid_gateway import PlaidError
from findata.sync import HISTORY_COMPLETE, describe_status, link_item, wait_for_history
from tests.conftest import FakeGateway, account, txn


def page(cursor, status, added=()):
    return {
        "added": list(added),
        "modified": [],
        "removed": [],
        "accounts": [account()],
        "next_cursor": cursor,
        "has_more": False,
        "transactions_update_status": status,
    }


def slow_history_gateway():
    """Mirrors what live Plaid does: INITIAL (empty) first, then the history arrives."""
    return FakeGateway(
        {
            None: page("c1", "INITIAL_UPDATE_COMPLETE"),
            "c1": page("c2", "INITIAL_UPDATE_COMPLETE"),
            "c2": page("c3", HISTORY_COMPLETE, added=[txn("t1"), txn("t2")]),
            "c3": page("c3", HISTORY_COMPLETE),
        }
    )


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def item(session, cipher):
    return link_item(session, FakeGateway(), cipher, "public-sandbox-ok")


def test_describe_status_is_plain_language():
    assert "still loading" in describe_status("INITIAL_UPDATE_COMPLETE")
    assert "still preparing" in describe_status("NOT_READY")
    assert "All available history" in describe_status(HISTORY_COMPLETE)
    assert "FUNKY" in describe_status("FUNKY")  # unknown statuses are shown, not hidden


def test_waits_through_initial_update_until_history_arrives(session, cipher, item):
    clock, polls = Clock(), []
    out = wait_for_history(
        session,
        slow_history_gateway(),
        cipher,
        item,
        timeout=60,
        interval=5,
        on_poll=lambda n, r, e: polls.append((n, r.status, r.added)),
        sleep=clock.sleep,
        clock=clock.monotonic,
    )
    assert out.history_complete and out.added == 2 and out.polls == 3
    assert polls == [
        (1, "INITIAL_UPDATE_COMPLETE", 0),  # the exact state that used to end the wait early
        (2, "INITIAL_UPDATE_COMPLETE", 0),
        (3, HISTORY_COMPLETE, 2),
    ]
    assert clock.sleeps == [5, 5]


def test_timeout_returns_incomplete_instead_of_raising(session, cipher, item):
    gw = FakeGateway(
        {
            None: page("c1", "INITIAL_UPDATE_COMPLETE"),
            **{f"c{i}": page(f"c{i + 1}", "INITIAL_UPDATE_COMPLETE") for i in range(1, 10)},
        }
    )
    clock = Clock()
    out = wait_for_history(
        session, gw, cipher, item, timeout=12, interval=5, sleep=clock.sleep, clock=clock.monotonic
    )
    assert not out.history_complete and out.status == "INITIAL_UPDATE_COMPLETE"
    assert out.elapsed <= 12 and out.polls == 3


def test_totals_accumulate_across_polls(session, cipher, item):
    gw = FakeGateway(
        {
            None: page("c1", "INITIAL_UPDATE_COMPLETE", added=[txn("t1")]),
            "c1": page("c2", HISTORY_COMPLETE, added=[txn("t2"), txn("t3")]),
        }
    )
    clock = Clock()
    out = wait_for_history(session, gw, cipher, item, sleep=clock.sleep, clock=clock.monotonic)
    assert out.added == 3 and out.polls == 2


# ---------- CLI ----------


@pytest.fixture
def run_cli(monkeypatch, engine, cipher, tmp_path):
    clock = Clock()
    monkeypatch.setattr("findata.sync.time.sleep", clock.sleep)
    monkeypatch.setattr("findata.sync.time.monotonic", clock.monotonic)
    holder = {"gateway": slow_history_gateway()}
    settings = Settings(findata_data_dir=tmp_path, _env_file=None)
    monkeypatch.setattr(
        cli,
        "_deps",
        lambda: (settings, make_session_factory(engine), holder["gateway"], cipher),
    )
    runner = CliRunner()
    return lambda *args: runner.invoke(cli.app, list(args)), holder


def test_sandbox_link_waits_and_explains(run_cli):
    invoke, _ = run_cli
    r = invoke("sandbox-link", "--interval", "5")
    assert r.exit_code == 0, r.output
    assert "Linked First Platypus Bank" in r.output
    assert "older history is still loading" in r.output  # explicit, not a silent +0
    assert "Done: 2 transactions loaded" in r.output
    assert "findata txns" in r.output  # tells the user what to do next


def test_sandbox_link_json_is_one_event_per_line(run_cli):
    invoke, _ = run_cli
    r = invoke("sandbox-link", "--json", "--interval", "5")
    assert r.exit_code == 0
    events = [json.loads(line) for line in r.output.splitlines()]  # every line parses
    assert [e["event"] for e in events] == [
        "linking",
        "linked",
        "waiting",
        "poll",
        "poll",
        "poll",
        "done",
    ]
    assert events[-1] == {
        "event": "done",
        "added": 2,
        "status": HISTORY_COMPLETE,
        "history_complete": True,
        "timed_out": False,
        "polls": 3,
    }


def test_sandbox_link_timeout_exits_2_with_next_step(run_cli):
    invoke, holder = run_cli
    holder["gateway"] = FakeGateway(
        {
            None: page("c1", "INITIAL_UPDATE_COMPLETE"),
            **{f"c{i}": page(f"c{i + 1}", "INITIAL_UPDATE_COMPLETE") for i in range(1, 30)},
        }
    )
    r = invoke("sandbox-link", "--timeout", "10", "--interval", "5")
    assert r.exit_code == 2
    assert "still loading history" in r.output and "findata sync" in r.output
    j = invoke("sandbox-link", "--json", "--timeout", "10", "--interval", "5")
    assert j.exit_code == 2
    assert json.loads(j.output.splitlines()[-1])["timed_out"] is True


def test_sandbox_link_plaid_error_is_explicit(run_cli):
    invoke, holder = run_cli
    holder["gateway"].sandbox_public_token = lambda username="": (_ for _ in ()).throw(
        PlaidError("INVALID_API_KEYS", "invalid client_id or secret provided", 400)
    )
    r = invoke("sandbox-link")
    assert r.exit_code == 1
    assert "INVALID_API_KEYS" in r.output
    j = invoke("sandbox-link", "--json")
    assert j.exit_code == 1
    assert json.loads(j.output.splitlines()[-1]) == {
        "event": "error",
        "message": "Plaid rejected the request (INVALID_API_KEYS): "
        "INVALID_API_KEYS: invalid client_id or secret provided",
        "error_code": "INVALID_API_KEYS",
    }


def test_missing_credentials_say_what_to_do(monkeypatch):
    def boom():
        raise RuntimeError("PLAID_CLIENT_ID and PLAID_SECRET are not set. Copy .env.example")

    monkeypatch.setattr(cli, "_deps", boom)
    r = CliRunner().invoke(cli.app, ["sync", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.output)["error_code"] == "NOT_CONFIGURED"


def test_sync_command_reports_status_per_item(run_cli):
    invoke, _ = run_cli
    assert "No linked items yet" in invoke("sync").output
    invoke("sandbox-link", "--interval", "5")
    r = invoke("sync")
    assert "All available history is loaded." in r.output
