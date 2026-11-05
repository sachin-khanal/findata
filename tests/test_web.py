"""The web front end: a forwarding app over DaemonClient (role `web`), never over the database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient

from findata.client import DaemonClient
from findata.client.errors import DaemonNotRunning, DaemonRejected
from findata.privfs import load_or_create_api_token
from findata.web.app import create_web_app
from findata.web.deps import TOKEN_HEADER
from findata.wire import SyncOut
from tests.conftest import FakeGateway, account, txn
from tests.test_daemon_events import _build, _eventually, _names, _published, serve
from tests.test_wait_and_cli import Clock

posix_only = pytest.mark.skipif(os.name != "posix", reason="AF_UNIX sockets")

UNSTARTED = {
    None: {
        "added": [],
        "modified": [],
        "removed": [],
        "accounts": [account()],
        "next_cursor": "c0",
        "has_more": False,
        "transactions_update_status": "INITIAL_UPDATE_COMPLETE",
    }
}


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
            },
            "c1": {
                "added": [],
                "modified": [],
                "removed": [],
                "accounts": [account()],
                "next_cursor": "c1",
                "has_more": False,
                "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            },
        }
    )


@pytest.fixture
def d(settings, keys, gateway):
    clock = Clock()  # a history job never sleeps for real
    return _build(settings, keys, gateway, sleep=clock.sleep, clock=clock.monotonic)


def web_client(d, **kw) -> DaemonClient:
    """A `web`-role client over the in-process daemon, sync and async."""
    return DaemonClient(
        d.locations.data_dir,
        "web",
        http_client=TestClient(d.app, base_url="http://daemon"),
        async_http_client=httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=d.app), base_url="http://daemon"
        ),
        **kw,
    )


def make_app(d, settings, client=None):
    token = load_or_create_api_token(settings.data_dir / "api.token")
    return create_web_app(client or web_client(d), token)


@pytest.fixture
def client(d, settings):
    return TestClient(make_app(d, settings), base_url="http://127.0.0.1")


@pytest.fixture
def auth(settings):
    return {TOKEN_HEADER: load_or_create_api_token(settings.data_dir / "api.token")}


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.json()["status"] == "ok" and r.json()["plaid_env"]


@pytest.mark.parametrize("path", ["/api/accounts", "/api/transactions", "/api/items"])
def test_api_requires_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={TOKEN_HEADER: "wrong"}).status_code == 401


def test_rejects_foreign_host_header(d, settings):
    c = TestClient(make_app(d, settings), base_url="http://evil.test")
    assert c.get("/api/health").status_code == 400


def test_index_injects_token(client, auth):
    html = client.get("/").text
    assert auth[TOKEN_HEADER] in html and "__FINDATA_TOKEN__" not in html
    assert "__PLAID_ENV__" not in html


def test_link_exchange_sync_and_query(client, auth, monkeypatch):
    # The exchange's history job would sync in the background and take the first page.
    monkeypatch.setattr(DaemonClient, "start_history", lambda self, item_id, **kw: "job")
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
            "history_complete": True,
            "detail": "All available history is loaded.",
        }
    ]

    accts = client.get("/api/accounts", headers=auth).json()
    assert accts[0]["current_balance"] == "100.0"  # money stays an exact decimal string

    txns = client.get("/api/transactions", headers=auth).json()
    assert [t["id"] for t in txns] == ["t1", "t2"]  # newest first
    only = client.get("/api/transactions", headers=auth, params={"start": "2026-09-02"}).json()
    assert [t["id"] for t in only] == ["t1"]
    assert [i["id"] for i in client.get("/api/items", headers=auth).json()] == ["item_1"]


def test_plaid_errors_surface_as_502(client, auth):
    r = client.post("/api/link/exchange", headers=auth, json={"public_token": "bad"})
    assert r.status_code == 502 and r.json()["error_code"] == "INVALID_PUBLIC_TOKEN"


def test_favicon_does_not_404(client):
    assert client.get("/favicon.ico").status_code == 204


# ---------- the daemon is down, slow or refuses ----------


class DownClient(DaemonClient):
    """Every call finds no daemon, as when `findata start` has not run."""

    def __init__(self, data_dir) -> None:
        super().__init__(data_dir, "web")

    def _connect(self):
        raise DaemonNotRunning("The findata daemon is not running. Start it with `findata start`.")


def test_web_returns_503_with_start_hint_when_daemon_is_down(settings, auth, d):
    down = DownClient(settings.data_dir)
    c = TestClient(make_app(d, settings, down), base_url="http://127.0.0.1")
    for path in ("/api/health", "/api/items", "/api/accounts", "/api/transactions"):
        r = c.get(path, headers=auth)
        assert r.status_code == 503, path
        assert r.json()["error_code"] == "DAEMON_NOT_RUNNING"
        assert "findata start" in r.json()["detail"]
    for path in ("/api/sync", "/api/link/token"):
        assert c.post(path, headers=auth).status_code == 503
    r = c.get("/api/events", headers=auth)
    assert r.status_code == 503 and r.json()["error_code"] == "DAEMON_NOT_RUNNING"
    # The page itself still loads, with the token and no Plaid environment filled in.
    page = c.get("/")
    assert page.status_code == 200 and auth[TOKEN_HEADER] in page.text
    assert "__PLAID_ENV__" not in page.text


def test_daemon_timeout_is_a_504_and_a_rejection_passes_through(d, settings, auth, monkeypatch):
    from findata.client.errors import DaemonTimeout

    c = TestClient(make_app(d, settings), base_url="http://127.0.0.1")

    def slow(self, **kw):
        raise DaemonTimeout("The findata daemon did not answer within 180 s.")

    monkeypatch.setattr(DaemonClient, "sync", slow)
    r = c.post("/api/sync", headers=auth)
    assert r.status_code == 504 and r.json()["error_code"] == "DAEMON_TIMEOUT"

    def refused(self, **kw):
        raise DaemonRejected("SCHEMA_BEHIND", "Run migrate.", 409, {"hint": "x"})

    monkeypatch.setattr(DaemonClient, "items", refused)
    r = c.get("/api/items", headers=auth)
    assert r.status_code == 409
    assert r.json() == {"error_code": "SCHEMA_BEHIND", "detail": "Run migrate.", "hint": "x"}


def test_web_app_never_imports_core_modules():
    code = (
        "import sys, findata.web.app\n"
        "core = ('db', 'security', 'plaid_gateway', 'sync', 'linking', 'waiting', 'queries',\n"
        "        'schema', 'runtime', 'contexts', 'config')\n"
        "print([m for m in core if f'findata.{m}' in sys.modules])\n"
    )
    out = subprocess.run(  # noqa: S603 (our own interpreter and a fixed script)
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# ---------- history jobs ----------


def test_link_exchange_starts_a_history_job(client, auth, d):
    r = client.post("/api/link/exchange", headers=auth, json={"public_token": "public-sandbox-ok"})
    assert r.status_code == 200
    assert set(r.json()) == {"id", "institution_name", "last_synced_at", "accounts", "transactions"}
    _eventually(lambda: "job_finished" in _names(d), "the history job")
    started = [e for e in _published(d) if e["event"] == "job_started"]
    assert [(e["kind"], e["item_id"]) for e in started] == [("history", "item_1")]


@pytest.mark.parametrize(
    "failure",
    [
        DaemonRejected("UNAVAILABLE", "try later", 503),
        DaemonNotRunning("The findata daemon is not running. Start it with `findata start`."),
    ],
)
def test_history_start_failures_do_not_fail_exchange_or_sync(
    client, auth, d, monkeypatch, failure, caplog
):
    def broken(self, item_id, **kw):
        raise failure

    monkeypatch.setattr(DaemonClient, "start_history", broken)
    r = client.post("/api/link/exchange", headers=auth, json={"public_token": "public-sandbox-ok"})
    assert r.status_code == 200 and r.json()["id"] == "item_1"
    monkeypatch.setattr(
        DaemonClient,
        "sync",
        lambda self, **kw: [
            SyncOut(
                item_id="item_1",
                added=1,
                modified=0,
                removed=0,
                status=None,
                history_complete=False,
                detail="loading",
            )
        ],
    )
    r = client.post("/api/sync", headers=auth)
    assert r.status_code == 200 and r.json()[0]["added"] == 1
    assert "Could not start a history job" in caplog.text


def test_exchange_succeeds_when_a_job_is_already_running(client, auth, d, monkeypatch):
    def busy(self, item_id, **kw):
        raise DaemonRejected("JOB_ALREADY_RUNNING", "A job is already running.", 409)

    monkeypatch.setattr(DaemonClient, "start_history", busy)
    r = client.post("/api/link/exchange", headers=auth, json={"public_token": "public-sandbox-ok"})
    assert r.status_code == 200 and r.json()["id"] == "item_1"


def test_sync_starts_a_history_job_while_history_is_loading(settings, keys, auth):
    clock = Clock()
    d = _build(settings, keys, FakeGateway(UNSTARTED), sleep=clock.sleep, clock=clock.monotonic)
    c = TestClient(make_app(d, settings), base_url="http://127.0.0.1")
    linked = d.client.post(  # straight to the daemon, so no job has started yet
        "/v1/link/exchange", headers=d.as_["cli"], json={"public_token": "public-sandbox-ok"}
    )
    assert linked.status_code == 200 and "job_started" not in _names(d)
    sync = c.post("/api/sync", headers=auth)
    assert sync.status_code == 200 and sync.json()[0]["history_complete"] is False
    _eventually(lambda: "job_started" in _names(d), "the history job")
    started = [e for e in _published(d) if e["event"] == "job_started"]
    assert [(e["kind"], e["item_id"]) for e in started] == [("history", "item_1")]


def test_sync_with_complete_history_starts_no_job(client, auth, d):
    client.post("/api/link/exchange", headers=auth, json={"public_token": "public-sandbox-ok"})
    _eventually(lambda: "job_finished" in _names(d), "the exchange's history job")
    before = _names(d).count("job_started")
    assert client.post("/api/sync", headers=auth).json()[0]["history_complete"] is True
    assert _names(d).count("job_started") == before


# ---------- events ----------


def test_web_events_relays_daemon_events_and_needs_the_token(client, auth, d):
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/events", headers={TOKEN_HEADER: "wrong"}).status_code == 401

    d.events.publish("item_removed", item_id="item_1")
    d.events.publish("item_linked", item_id="item_2", institution="Bank")
    d.events.close()  # TestClient buffers a whole response, so the stream must end
    r = client.get("/api/events", headers=auth, params={"after": 0})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in r.text.splitlines() if line]
    assert [(e["seq"], e["event"]) for e in lines] == [(1, "item_removed"), (2, "item_linked")]


def test_served_page_is_one_page(client):
    html = client.get("/").text
    assert html.count("<script>") == 1 and html.count("<script") == 2  # inline + Plaid's
    assert html.count('id="status"') == 1 and html.count("const TOKEN") == 1
    assert html.count("</html>") == 1 and html.rstrip().endswith("</html>")


def test_page_has_no_retry_loop():
    from importlib.resources import files

    html = files("findata.web").joinpath("index.html").read_text()
    for banned in ("POLL_MS", "MAX_POLLS", "setInterval", "setTimeout", "sleep(", "EventSource"):
        assert banned not in html, banned
    assert "/api/events" in html and "getReader" in html and "Reconnect" in html


@posix_only
def test_browser_disconnect_frees_the_daemon_stream_slot(settings, keys, gateway):
    d = _build(settings, keys, gateway)
    with serve(d) as daemon_sock:
        upstream = DaemonClient(
            d.locations.data_dir,
            "web",
            async_http_client=httpx2.AsyncClient(
                transport=httpx2.AsyncHTTPTransport(uds=str(daemon_sock)), base_url="http://daemon"
            ),
        )
        token = load_or_create_api_token(settings.data_dir / "api.token")
        web = SimpleNamespace(app=create_web_app(upstream, token), events=d.events)
        with serve(web) as web_sock:
            browser = httpx2.Client(
                transport=httpx2.HTTPTransport(uds=str(web_sock)),
                base_url="http://127.0.0.1",
                timeout=5,
            )
            with browser.stream("GET", "/api/events", headers={TOKEN_HEADER: token}) as r:
                assert r.status_code == 200
                _eventually(lambda: len(d.events._streams) == 1, "the daemon-side stream to open")
                d.events.publish("item_removed", item_id="item_1")
                line = next(line for line in r.iter_lines() if line)
                assert json.loads(line)["event"] == "item_removed"
            # The browser has gone: the relay must close its stream to the daemon.
            _eventually(lambda: len(d.events._streams) == 0, "the daemon-side slot to be freed")
            browser.close()


@posix_only
def test_shutdown_ends_open_event_streams_cleanly(settings, keys, gateway, caplog):
    import shutil
    import socket
    import tempfile
    import threading
    import time
    from pathlib import Path

    from findata.web.app import WebServer

    caplog.set_level("INFO")
    d = _build(settings, keys, gateway)
    with serve(d) as daemon_sock:
        upstream = DaemonClient(
            d.locations.data_dir,
            "web",
            async_http_client=httpx2.AsyncClient(
                transport=httpx2.AsyncHTTPTransport(uds=str(daemon_sock)), base_url="http://daemon"
            ),
        )
        token = load_or_create_api_token(settings.data_dir / "api.token")
        folder = Path(tempfile.mkdtemp(dir="/tmp", prefix="fw"))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(folder / "w.sock"))
        sock.listen()
        server = WebServer(
            create_web_app(upstream, token),
            lifespan="off",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        browser = httpx2.Client(
            transport=httpx2.HTTPTransport(uds=str(folder / "w.sock")),
            base_url="http://127.0.0.1",
            timeout=10,
        )
        try:
            with browser.stream("GET", "/api/events", headers={TOKEN_HEADER: token}) as r:
                assert r.status_code == 200
                _eventually(lambda: len(d.events._streams) == 1, "the daemon-side stream to open")
                started = time.monotonic()
                server.should_exit = True  # what Ctrl+C does
                assert list(r.iter_lines()) == []  # the stream ends by itself
            thread.join(10)
            assert not thread.is_alive()
            assert time.monotonic() - started < 3  # well under the 5 s graceful timeout
            _eventually(lambda: len(d.events._streams) == 0, "the daemon-side slot to be freed")
            assert "Exception in ASGI application" not in caplog.text
            assert "CancelledError" not in caplog.text and "Cancel" not in caplog.text
        finally:
            browser.close()
            sock.close()
            shutil.rmtree(folder, ignore_errors=True)
