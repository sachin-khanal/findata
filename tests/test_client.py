"""DaemonClient: typed calls over the real daemon app, and explicit failure modes."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from findata.client import DaemonClient
from findata.client.errors import DaemonClientError, DaemonNotRunning, DaemonRejected, DaemonTimeout
from findata.client.liveness import daemon_lock_held
from findata.locations import Locations
from findata.wire import GrantOut, ItemOut, JobOut, SandboxLinkIn, StatusOut, TransactionOut
from tests.test_daemon_api import OverlapGateway, _build, _pages

posix_only = pytest.mark.skipif(os.name != "posix", reason="AF_UNIX sockets")


@pytest.fixture
def gateway():
    return OverlapGateway(_pages())


@pytest.fixture
def d(settings, keys, gateway):
    return _build(settings, keys, gateway)


def make_client(d, role="cli", **kw) -> DaemonClient:
    http = TestClient(d.client.app, base_url="http://daemon")
    return DaemonClient(d.locations.data_dir, role, http_client=http, **kw)


def test_client_round_trips_items_and_transactions(d):
    c = make_client(d)
    assert c.items() == []
    item = c.exchange("public-sandbox-ok")
    assert isinstance(item, ItemOut)
    assert c.link_token() == "link-sandbox-abc"
    results = c.sync()
    assert results[0].added == 2
    assert [i.id for i in c.items()] == [item.id]
    assert c.accounts()[0].id == "acc_1"
    txns = c.transactions(account_id="acc_1", category="FOOD_AND_DRINK", limit=10)
    assert len(txns) == 2 and isinstance(txns[0], TransactionOut)
    only = c.transactions(start=dt.date(2026, 9, 2), end=dt.date(2026, 9, 2))
    assert [t.id for t in only] == ["t1"]
    assert isinstance(c.status(), StatusOut)
    assert c.audit(5)
    c.remove_item(item.id, local_only=True)
    assert c.items() == []


def test_grants_approvals_and_shutdown(d):
    c = make_client(d)
    assert c.current_grant() is None
    g = c.grant(["items.remove"], 60)
    assert isinstance(g, GrantOut) and g.operations == ["items.remove"]
    assert c.current_grant() == g
    c.revoke()
    assert c.current_grant() is None
    assert c.approvals() == []
    with pytest.raises(DaemonRejected) as e:
        c.approve("nope")
    assert e.value.error_code == "NOT_FOUND" and e.value.status == 404
    c.shutdown()
    assert d.shutdowns == [True]


def test_forbidden_write_surfaces_error_code_and_detail(d):
    cli = make_client(d)
    item = cli.exchange("public-sandbox-ok")
    mcp = make_client(d, "mcp")
    with pytest.raises(DaemonRejected) as e:
        mcp.sync()
    assert e.value.status == 403 and e.value.error_code == "WRITE_NOT_GRANTED" and e.value.detail
    cli.grant(["items.remove"], 60)
    with pytest.raises(DaemonRejected) as e:
        mcp.remove_item(item.id)
    assert e.value.error_code == "CONFIRMATION_REQUIRED"
    rid = e.value.extra["request_id"]
    cli.approve(rid)
    mcp.remove_item(item.id, request_id=rid)
    assert cli.items() == []


def test_wrong_credential_raises_daemon_rejected_unauthenticated(d, tmp_path):
    http = TestClient(d.client.app, base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path, "wrong-token"), "cli", http_client=http)
    with pytest.raises(DaemonRejected) as e:
        c.items()
    assert e.value.error_code == "UNAUTHENTICATED" and e.value.status == 401
    assert "wrong-token" not in str(e.value) and "wrong-token" not in repr(c)


def test_missing_credential_file_names_the_file_and_fix(tmp_path):
    c = DaemonClient(tmp_path / "data", "cli")  # constructing never fails
    with pytest.raises(DaemonClientError) as e:
        c.items()
    assert not isinstance(e.value, DaemonNotRunning)
    msg = str(e.value)
    assert "cli.token" in msg and "findata init" in msg


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_loose_credential_file_propagates_permission_error(d):
    path = d.locations.credentials_dir / "cli.token"
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        make_client(d).items()


def test_empty_credential_file_names_the_file(d):
    (d.locations.credentials_dir / "cli.token").write_text("")
    with pytest.raises(DaemonClientError, match=r"cli\.token"):
        make_client(d).items()


def test_credential_is_cached_and_repr_hides_it(d):
    c = make_client(d)
    c.items()
    token = d.tokens["cli"]
    assert c._credential == token  # read once, then cached
    assert token not in repr(c)


def _short_dir() -> Path:
    return Path(tempfile.mkdtemp(dir="/tmp", prefix="fc"))


@posix_only
def test_missing_socket_raises_daemon_not_running():
    data = _short_dir()
    try:
        locs = Locations(data)
        locs.credentials_dir.mkdir(mode=0o700)
        tok = locs.credentials_dir / "cli.token"
        tok.write_text("t")
        tok.chmod(0o600)
        started = time.monotonic()
        with pytest.raises(DaemonNotRunning, match="findata start"):
            DaemonClient(data, "cli", timeout=5).items()
        assert time.monotonic() - started < 2
    finally:
        shutil.rmtree(data, ignore_errors=True)


@posix_only
def test_socket_file_with_nothing_listening_raises_daemon_not_running():
    data = _short_dir()
    try:
        locs = Locations(data)
        locs.credentials_dir.mkdir(mode=0o700)
        tok = locs.credentials_dir / "cli.token"
        tok.write_text("t")
        tok.chmod(0o600)
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(locs.socket))
        s.close()  # the file stays; nobody listens
        assert locs.socket.exists()
        started = time.monotonic()
        with pytest.raises(DaemonNotRunning, match="findata start"):
            DaemonClient(data, "cli", timeout=5).items()
        assert time.monotonic() - started < 2
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_timeout_maps_to_daemon_timeout_not_not_running(tmp_path):
    def slow(request):
        raise httpx2.ReadTimeout("slow", request=request)

    http = httpx2.Client(transport=httpx2.MockTransport(slow), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", timeout=3, http_client=http)
    with pytest.raises(
        DaemonTimeout, match=r"did not answer within 3 s.*may still be running"
    ) as e:
        c.items()
    assert not isinstance(e.value, DaemonNotRunning)


def _cred_dir(tmp_path, token: str = "tok", name: str = "client-data") -> Path:  # noqa: S107
    data = tmp_path / name
    (data / "credentials").mkdir(parents=True, mode=0o700)
    tok = data / "credentials" / "cli.token"
    tok.write_text(token)
    tok.chmod(0o600)
    return data


def test_job_methods_with_mock_transport(tmp_path):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, request.headers["X-Findata-Credential"]))
        if request.method == "POST":
            assert json.loads(request.content)["username"] == "u"
            return httpx2.Response(202, json={"job_id": "j1"})
        return httpx2.Response(200, json={"id": "j1", "state": "done", "result": {"a": 1}})

    http = httpx2.Client(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", http_client=http)
    assert c.start_sandbox_link(SandboxLinkIn(username="u")) == "j1"
    job = c.job("j1")
    assert isinstance(job, JobOut) and job.state == "done"
    assert seen == [
        ("POST", "/v1/jobs/sandbox-link", "tok"),
        ("GET", "/v1/jobs/j1", "tok"),
    ]


def test_non_json_error_body_is_bad_response(tmp_path):
    http = httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(502, text="<html>")),
        base_url="http://daemon",
    )
    c = DaemonClient(_cred_dir(tmp_path), "cli", http_client=http)
    with pytest.raises(DaemonRejected) as e:
        c.items()
    assert e.value.error_code == "BAD_RESPONSE" and e.value.status == 502


def test_importing_the_client_does_not_load_daemon_internals():
    banned = "db security plaid_gateway sync linking waiting queries schema runtime contexts config"
    code = (
        "import sys, findata.client\n"
        f"bad = [m for m in '{banned}'.split() if f'findata.{{m}}' in sys.modules]\n"
        "assert not bad, bad\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603
    assert r.returncode == 0, r.stderr


def test_quick_and_long_calls_use_their_own_timeouts(tmp_path):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen[request.url.path] = request.extensions["timeout"]["read"]
        if request.url.path == "/v1/sync":
            return httpx2.Response(200, json=[])
        return httpx2.Response(200, json=[])

    http = httpx2.Client(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", timeout=4, long_timeout=77, http_client=http)
    c.items()
    c.sync()
    c.sync(timeout=5)
    c.items(timeout=9)
    assert seen["/v1/sync"] == 5 and seen["/v1/items"] == 9
    seen.clear()
    c.items()
    c.sync()
    assert seen == {"/v1/items": 4, "/v1/sync": 77}


def test_path_segments_are_quoted(tmp_path):
    paths = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append((request.url.raw_path.decode(), request.url.query))
        return httpx2.Response(204)

    http = httpx2.Client(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", http_client=http)
    c.remove_item("../x")
    c.remove_item("a?b")
    c.approve("a/b#c")
    assert paths[0][0].startswith("/v1/items/..%2Fx?")
    assert paths[1][0].startswith("/v1/items/a%3Fb?")
    assert paths[2][0] == "/v1/approvals/a%2Fb%23c"


def test_daemon_lock_held_detects_a_real_holder(tmp_path):
    lock = tmp_path / "daemon.lock"
    assert daemon_lock_held(lock) is False  # missing file
    lock.write_bytes(b"")
    assert daemon_lock_held(lock) is False  # unheld
    code = (
        "import fcntl, sys, time\n"
        f"f = open({str(lock)!r}, 'r+')\n"
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('held', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        assert proc.stdout.readline().strip() == "held"
        assert daemon_lock_held(lock) is True
    finally:
        proc.kill()
        proc.wait()
    assert daemon_lock_held(lock) is False  # released with the process


def test_tcp_branch_refuses_to_send_when_no_daemon_holds_the_lock(tmp_path):
    data = _cred_dir(tmp_path)
    locs = Locations(data)
    locs.port_file.write_text("9")  # stale port file
    locs.lock.write_bytes(b"")  # lock file exists but nobody holds it
    c = DaemonClient(data, "cli")
    with pytest.raises(DaemonNotRunning):
        c._connect_tcp()
    locs.lock.unlink()
    with pytest.raises(DaemonNotRunning):
        c._connect_tcp()
