"""One real daemon on a real Unix socket, driven through DaemonClient as each role."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from findata.client import DaemonClient
from findata.client.errors import DaemonRejected
from findata.config import Settings
from findata.daemon.server import run_daemon
from findata.locations import Locations
from findata.web.app import create_web_app
from findata.web.session import WebSessions
from tests.conftest import FakeGateway, FakePresence
from tests.test_client import posix_only
from tests.test_daemon_api import _pages
from tests.test_daemon_server import _wait_until_ready


class EventWatcher:
    """Reads `/v1/events` as `web` on its own thread and lets a test wait for a named event."""

    def __init__(self, client: DaemonClient) -> None:
        self._stream = client.events()  # opened now: only later events are seen
        self.seen: list[dict] = []
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        with contextlib.suppress(Exception):  # the stream ends when the daemon stops
            for event in self._stream:
                with self._cond:
                    self.seen.append(event)
                    self._cond.notify_all()

    def wait_for(self, name: str, timeout: float = 10.0) -> dict:
        with self._cond:
            ok = self._cond.wait_for(lambda: any(e["event"] == name for e in self.seen), timeout)
            assert ok, f"no {name!r} event within {timeout}s; saw {[e['event'] for e in self.seen]}"
            return next(e for e in self.seen if e["event"] == name)

    def join(self) -> None:
        self._thread.join(timeout=10)


@posix_only
def test_real_socket_end_to_end():
    data_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="fe"))  # short: AF_UNIX path limit
    settings = Settings(
        plaid_client_id="test",
        plaid_secret="test",
        findata_data_dir=data_dir,
        findata_key_backend="file",
        _env_file=None,
    )
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            run_daemon(settings, presence=FakePresence("confirmed"), gateway=FakeGateway(_pages()))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    locations = Locations(data_dir)
    watcher: EventWatcher | None = None
    web_app_client: DaemonClient | None = None
    clients: list[DaemonClient] = []
    try:
        thread.start()
        _wait_until_ready(locations, thread.is_alive)
        cli, web, mcp = (DaemonClient(data_dir, role) for role in ("cli", "web", "mcp"))
        clients = [cli, web, mcp]

        # Reads work for every role.
        item = cli.exchange("public-sandbox-ok")
        for c in clients:
            assert [i.id for i in c.items()] == [item.id]
            assert c.status().version

        # Events opened as web see a sync finish.
        watcher = EventWatcher(web)
        assert cli.sync()[0].added == 2
        assert watcher.wait_for("sync_completed")["item_id"] == item.id
        for c in clients:
            assert c.accounts() and len(c.transactions(limit=10)) == 2

        # An mcp write is refused until a human grants it.
        with pytest.raises(DaemonRejected) as refused:
            mcp.sync()
        assert refused.value.error_code == "WRITE_NOT_GRANTED"
        cli.grant(["sync"], 600)
        assert mcp.sync()[0].item_id == item.id
        cli.revoke()
        with pytest.raises(DaemonRejected) as refused:
            mcp.sync()
        assert refused.value.error_code == "WRITE_NOT_GRANTED"

        # A short grant ends on its own; the daemon says so, and the write is refused again.
        cli.grant(["sync"], 1)
        watcher.wait_for("grant_expired")
        with pytest.raises(DaemonRejected) as refused:
            mcp.sync()
        assert refused.value.error_code == "WRITE_NOT_GRANTED"

        # A wrong credential is rejected.
        with httpx2.Client(
            transport=httpx2.HTTPTransport(uds=str(locations.socket)), base_url="http://daemon"
        ) as raw:
            bad = raw.get("/v1/items", headers={"X-Findata-Credential": "not-a-credential"})
            assert bad.status_code == 401
            none = raw.get("/v1/items")
            assert none.status_code == 401

        # The web front end, on top of the real daemon: no session, no data.
        web_app_client = DaemonClient(data_dir, "web")
        sessions = WebSessions()
        browser = TestClient(create_web_app(web_app_client, sessions), base_url="http://127.0.0.1")
        assert browser.get("/api/items").status_code == 401
        secret = sessions.launch_url("127.0.0.1", 8765).split("#launch=")[1]
        login = browser.post("/api/session", json={"launch": secret})
        assert login.status_code == 200
        shown = browser.get(
            "/api/items", headers={"X-Findata-Session": login.json()["request_key"]}
        )
        assert shown.status_code == 200
        assert [i["id"] for i in shown.json()] == [item.id]
    finally:
        try:
            if thread.is_alive():
                with contextlib.suppress(Exception):
                    DaemonClient(data_dir, "cli").shutdown()
            thread.join(timeout=20)
            if watcher is not None:
                watcher.join()
            for c in [*clients, *([web_app_client] if web_app_client else [])]:
                c.close()
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)
    assert not thread.is_alive(), "the daemon did not stop"
    assert not errors, errors
