"""DaemonClient's streams: follow one job, follow the daemon's events, and how they fail."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time

import httpx2
import pytest

from findata.client import DaemonClient
from findata.client.errors import DaemonNotRunning, DaemonRejected
from findata.locations import Locations
from findata.wire import SandboxLinkIn
from tests.conftest import FakeGateway
from tests.test_client import _cred_dir, _short_dir, make_client, posix_only
from tests.test_daemon_events import _build, serve
from tests.test_wait_and_cli import Clock, slow_history_gateway


@pytest.fixture
def d(settings, keys):
    clock = Clock()
    return _build(settings, keys, slow_history_gateway(), sleep=clock.sleep, clock=clock.monotonic)


def _ndjson(*objects: dict) -> bytes:
    return b"".join(json.dumps(o).encode() + b"\n" for o in objects)


def _mock(tmp_path, handler, **kw) -> DaemonClient:
    http = httpx2.Client(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    return DaemonClient(_cred_dir(tmp_path), "cli", http_client=http, **kw)


def test_follow_job_yields_events_until_finished(d):
    c = make_client(d)
    job_id = c.start_sandbox_link(SandboxLinkIn(interval=5))
    events = list(c.follow_job(job_id))
    job = c.job(job_id)
    assert job.state == "done"
    assert events[:-1] == job.events  # the job's own event dicts, as the CLI prints them
    assert events[-1] == {"event": "job_finished", "state": "done", "result": job.result}
    with pytest.raises(DaemonRejected) as e:
        c.follow_job("nope")  # refused when called, before any iteration
    assert e.value.error_code == "NOT_FOUND" and e.value.status == 404


def test_follow_job_raises_if_the_stream_ends_before_the_job_finished(tmp_path):
    body = _ndjson({"event": "linking", "username": "u"}, {"event": "keepalive"})
    c = _mock(tmp_path, lambda request: httpx2.Response(200, content=body))
    events = c.follow_job("j1")
    assert next(events) == {"event": "linking", "username": "u"}
    with pytest.raises(DaemonNotRunning, match="stopped before the job finished"):
        next(events)


def test_events_iterator_skips_keepalives(tmp_path, settings, keys):
    first = {"seq": 1, "event": "grant_revoked", "at": "2026-10-21T09:00:00Z"}
    second = {"seq": 2, "event": "item_removed", "at": "2026-10-21T09:00:00Z", "item_id": "i"}
    keepalive = {"event": "keepalive"}
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = _ndjson(keepalive, first, keepalive, keepalive, second, keepalive)
        return httpx2.Response(200, content=body, headers={"content-type": "application/x-ndjson"})

    c = _mock(tmp_path, handler, timeout=4)
    assert list(c.events(after=0)) == [first, second]
    assert list(c.events()) == [first, second]
    assert [(r.url.path, dict(r.url.params)) for r in seen] == [
        ("/v1/events", {"after": "0"}),
        ("/v1/events", {}),
    ]
    # Only the connect (and write) timeout applies: an idle stream is not a slow daemon.
    assert seen[0].extensions["timeout"] == {"connect": 4, "read": None, "write": 4, "pool": 4}

    # Against a real server sending keep-alives every 50 ms, the iterator yields only events.
    d = _build(settings, keys, FakeGateway(), keepalive_s=0.05)
    with serve(d) as sock:
        http = httpx2.Client(transport=httpx2.HTTPTransport(uds=str(sock)), base_url="http://x")
        live = DaemonClient(d.locations.data_dir, "cli", http_client=http).events()
        time.sleep(0.2)  # several keep-alives go by
        d.events.publish("item_removed", item_id="item_1")
        event = next(live)
        assert (event["seq"], event["event"], event["item_id"]) == (1, "item_removed", "item_1")
        live.close()
        http.close()


@posix_only
def test_streams_report_daemon_not_running():
    data = _short_dir()
    try:
        locs = Locations(data)
        locs.credentials_dir.mkdir(mode=0o700)
        tok = locs.credentials_dir / "cli.token"
        tok.write_text("t")
        tok.chmod(0o600)
        for socket_file in (False, True):  # no socket, then a socket file nobody listens on
            if socket_file:
                s = socket.socket(socket.AF_UNIX)
                s.bind(str(locs.socket))
                s.close()
                assert locs.socket.exists()
            c = DaemonClient(data, "cli", timeout=5)
            started = time.monotonic()
            with pytest.raises(DaemonNotRunning, match="findata start"):
                c.events()  # raised by the call itself, before iterating
            with pytest.raises(DaemonNotRunning, match="findata start"):
                c.follow_job("j1")
            assert time.monotonic() - started < 2
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_a_daemon_that_dies_mid_stream_is_daemon_not_running(tmp_path):
    def body():
        yield _ndjson({"seq": 1, "event": "grant_revoked", "at": "2026-10-21T09:00:00Z"})
        raise httpx2.ReadError("connection reset")

    c = _mock(tmp_path, lambda request: httpx2.Response(200, content=body()))
    events = c.events()
    assert next(events)["seq"] == 1
    with pytest.raises(DaemonNotRunning):
        next(events)


def test_stream_refusals_raise_daemon_rejected(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            503, json={"error_code": "TOO_MANY_STREAMS", "detail": "Too many streams."}
        )

    with pytest.raises(DaemonRejected) as e:
        _mock(tmp_path, handler).events()
    assert e.value.error_code == "TOO_MANY_STREAMS" and e.value.status == 503


def test_start_history_posts_the_item_and_returns_the_job_id(tmp_path):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx2.Response(202, json={"job_id": "j1"})

    assert _mock(tmp_path, handler).start_history("item_1") == "j1"
    assert seen == [("POST", "/v1/jobs/history", {"item_id": "item_1"})]


def test_aevents_skips_or_keeps_keepalives_and_maps_refusals(tmp_path):
    first = {"seq": 1, "event": "item_removed", "at": "2026-10-21T09:00:00Z", "item_id": "i"}
    body = _ndjson({"event": "keepalive"}, first)
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.extensions["timeout"])
        if request.url.params.get("after") == "9":
            return httpx2.Response(409, json={"error_code": "TOO_MANY", "detail": "full"})
        return httpx2.Response(200, content=body)

    ahttp = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", async_http_client=ahttp, timeout=4)

    async def run():
        plain = [e async for e in await c.aevents()]
        kept = [e async for e in await c.aevents(keepalives=True)]
        with pytest.raises(DaemonRejected) as e:
            await c.aevents(after=9)  # refused when awaited, before any iteration
        return plain, kept, e.value

    plain, kept, refused = asyncio.run(run())
    assert plain == [first] and kept == [{"event": "keepalive"}, first]
    assert seen[0] == {"connect": 4, "read": None, "write": 4, "pool": 4}
    assert refused.error_code == "TOO_MANY" and refused.status == 409


def test_aevents_without_a_daemon_raises_not_running(tmp_path):
    with pytest.raises(DaemonNotRunning):
        asyncio.run(DaemonClient(_cred_dir(tmp_path), "cli").aevents())
