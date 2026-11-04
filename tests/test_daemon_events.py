"""Daemon events and job progress pushed as NDJSON streams, so no findata process polls another.

Streams that must stay open are read from a real server on a Unix socket: Starlette's
TestClient buffers a whole response, so it can only read streams that end (a job's, or any
stream once the bus is closed). No test waits real time for a timer: grants and approvals get
fake timers that the test fires once the fake clock reaches the deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from findata.contexts import token_context
from findata.daemon.app import create_daemon_app
from findata.daemon.audit import AuditLog
from findata.daemon.auth import CredentialStore
from findata.daemon.events import EventBus, TooManyStreams
from findata.daemon.grants import ApprovalStore, GrantStore
from findata.db import Item, make_engine, make_session_factory
from findata.locations import Locations
from findata.runtime import Runtime
from findata.schema import ensure_current
from findata.sync import HISTORY_COMPLETE
from tests.conftest import ACCESS_TOKEN, FakeGateway, FakePresence, account, txn
from tests.test_daemon_api import CRED, ROLES, FakeClock, _error, _pages
from tests.test_daemon_server import ThreadedDaemon, _settings, _shutdown, _token
from tests.test_wait_and_cli import Clock, slow_history_gateway

posix_only = pytest.mark.skipif(os.name != "posix", reason="AF_UNIX sockets")

DONE = {
    "added": 2,
    "status": HISTORY_COMPLETE,
    "history_complete": True,
    "timed_out": False,
    "polls": 4,
}


class FakeTimer:
    def __init__(self, delay_s: float, fn) -> None:
        self.delay_s = delay_s
        self._fn = fn
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        """The timer's deadline has come (a cancelled timer never fires)."""
        if not self.cancelled:
            self._fn()

    def fire_late(self) -> None:
        """As a timer thread that was already running when it was cancelled."""
        self._fn()


class FakeTimers:
    """Stands in for `start_timer`: records every timer and lets the test fire it."""

    def __init__(self) -> None:
        self.started: list[FakeTimer] = []

    def __call__(self, delay_s: float, fn) -> FakeTimer:
        timer = FakeTimer(delay_s, fn)
        self.started.append(timer)
        return timer


class Steps:
    """A job's sleep between polls that waits for the test to let each one pass."""

    def __init__(self) -> None:
        self.clock = Clock()
        self._paused = threading.Semaphore(0)
        self._go = threading.Semaphore(0)
        self._free = False

    def sleep(self, seconds: float) -> None:
        self._paused.release()
        if not self._free:
            assert self._go.acquire(timeout=10), "the test never let the job go on"
        self.clock.sleep(seconds)

    def wait_paused(self) -> None:
        assert self._paused.acquire(timeout=10), "the job never reached its next sleep"

    def step(self) -> None:
        self._go.release()

    def release(self) -> None:
        self._free = True
        self._go.release()


@pytest.fixture
def steps():
    s = Steps()
    yield s
    s.release()  # never leave a job thread waiting on a failed test


def _build(settings, keys, gateway, *, events=None, sleep=None, clock=None, keepalive_s=15.0):
    engine = make_engine(settings.db_path, keys.db_key)
    ensure_current(engine)
    runtime = Runtime(settings, make_session_factory(engine), keys.cipher, gateway)
    locations = Locations(settings.data_dir)
    credentials = CredentialStore(locations)
    credentials.ensure_all()
    fake = FakeClock()
    events = EventBus(clock=fake) if events is None else events
    timers = FakeTimers()
    grants = GrantStore(fake, events=events, timer=timers)
    approvals = ApprovalStore(fake, events=events, timer=timers)
    app = create_daemon_app(
        runtime,
        credentials,
        grants,
        approvals,
        AuditLog(runtime.session_factory, fake),
        started_at=fake(),
        clock=fake,
        presence=FakePresence("confirmed"),
        job_sleep=sleep,
        job_clock=clock,
        events=events,
        keepalive_s=keepalive_s,
    )
    tokens = {r: (locations.credentials_dir / f"{r}.token").read_text().strip() for r in ROLES}
    return SimpleNamespace(
        app=app,
        client=TestClient(app, base_url="http://127.0.0.1"),
        as_={r: {CRED: t} for r, t in tokens.items()},
        tokens=tokens,
        events=events,
        grants=grants,
        approvals=approvals,
        timers=timers,
        clock=fake,
        engine=engine,
        session_factory=runtime.session_factory,
        locations=locations,
    )


@pytest.fixture
def d(settings, keys):
    clock = Clock()
    return _build(settings, keys, slow_history_gateway(), sleep=clock.sleep, clock=clock.monotonic)


@contextlib.contextmanager
def serve(d) -> Iterator[Path]:
    """Serve `d.app` on a Unix socket, as `findata start` does; yields the socket's path."""
    folder = Path(tempfile.mkdtemp(dir="/tmp", prefix="fe"))  # socket paths must be short
    path = folder / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen()  # connections queue until the server accepts them: no readiness wait
    config = uvicorn.Config(
        d.app,
        lifespan="off",
        log_config=None,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        d.events.close()  # ends any stream a failing test left open
        server.should_exit = True
        thread.join(10)
        sock.close()
        shutil.rmtree(folder, ignore_errors=True)


def _http(sock: Path) -> httpx2.Client:
    transport = httpx2.HTTPTransport(uds=str(sock))
    return httpx2.Client(transport=transport, base_url="http://daemon", timeout=5)


class Lines:
    """An open stream read one line at a time; a read waits at most 5 s."""

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self._lines = response.iter_lines() if response.status_code == 200 else iter(())

    def next(self) -> dict:
        for line in self._lines:
            if line:
                return json.loads(line)
        raise AssertionError("the stream ended")

    def rest(self) -> list[dict]:
        """Every line until the stream ends."""
        return [json.loads(line) for line in self._lines if line]


@contextlib.contextmanager
def stream(sock: Path, path: str, headers: dict, **params) -> Iterator[Lines]:
    with _http(sock) as http, http.stream("GET", path, params=params, headers=headers) as r:
        yield Lines(r)


def _refused(lines: Lines, status: int, code: str) -> dict:
    r = lines.response
    r.read()
    return _error(r, status, code)


def _published(d) -> list[dict]:
    """Every event the daemon has published so far, as a stream sends them."""
    _, lines, _ = d.events.read(0)
    return [json.loads(line) for line in lines.splitlines()]


def _names(d) -> list[str]:
    return [e["event"] for e in _published(d)]


def _fields(event: dict) -> dict:
    return {k: v for k, v in event.items() if k not in ("seq", "at")}


def _last(d, name: str) -> dict:
    return _fields([e for e in _published(d) if e["event"] == name][-1])


def _job(d, job_id: str) -> dict:
    r = d.client.get(f"/v1/jobs/{job_id}", headers=d.as_["cli"])
    assert r.status_code == 200, r.text
    return r.json()


def _job_stream(d, job_id: str, role: str = "cli") -> list[dict]:
    """A job's whole stream; TestClient returns it once the job has finished."""
    r = d.client.get(f"/v1/jobs/{job_id}/events", headers=d.as_[role])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/x-ndjson"
    return [json.loads(line) for line in r.text.splitlines() if line]


def _start(d, role: str = "cli", **body) -> str:
    r = d.client.post("/v1/jobs/sandbox-link", headers=d.as_[role], json=body)
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _start_history(d, item_id: str = "item_1", role: str = "cli") -> str:
    r = d.client.post("/v1/jobs/history", headers=d.as_[role], json={"item_id": item_id})
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"job_id"}
    return r.json()["job_id"]


def _exchange(d) -> None:
    r = d.client.post(
        "/v1/link/exchange", headers=d.as_["cli"], json={"public_token": "public-sandbox-ok"}
    )
    assert r.status_code == 200, r.text


def _add_item(d, keys, item_id: str, token: str = ACCESS_TOKEN, institution=None) -> None:
    """An Item stored straight in the database, as if it had been linked."""
    with d.session_factory() as s:
        enc = keys.cipher.encrypt(token, token_context(item_id))
        s.add(Item(id=item_id, institution_name=institution, access_token_enc=enc))
        s.commit()


def _eventually(condition, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


# ---------- job streams ----------


def test_job_stream_replays_then_follows_and_ends_with_job_finished(settings, keys, steps):
    d = _build(
        settings, keys, slow_history_gateway(), sleep=steps.sleep, clock=steps.clock.monotonic
    )
    job_id = _start(d, interval=5)
    steps.wait_paused()  # after the first poll, before the second
    with serve(d) as sock, stream(sock, f"/v1/jobs/{job_id}/events", d.as_["cli"]) as s:
        assert s.response.headers["content-type"] == "application/x-ndjson"
        replayed = [s.next() for _ in range(4)]
        assert replayed == _job(d, job_id)["events"]  # what was recorded before it opened
        steps.step()
        steps.wait_paused()  # the second poll is done and the job sleeps again
        live = s.next()
        assert live["event"] == "poll" and live["attempt"] == 2
        assert _job(d, job_id)["state"] == "running"  # it came while the job was still running
        steps.release()
        rest = s.rest()  # the stream ends by itself after job_finished
    job = _job(d, job_id)
    assert job["state"] == "done"
    assert [*replayed, live, *rest[:-1]] == job["events"]  # identical to GET /v1/jobs/{id}
    assert rest[-1] == {"event": "job_finished", "state": "done", "result": job["result"]}


def test_job_stream_keeps_the_cli_event_sequence(d):
    lines = _job_stream(d, _start(d, interval=5))
    assert [e["event"] for e in lines] == [
        "linking", "linked", "waiting", "poll", "poll", "poll", "poll", "done", "job_finished",
    ]  # fmt: skip
    assert lines[0] == {"event": "linking", "username": "user_transactions_dynamic"}
    assert lines[-2] == {"event": "done", **DONE}
    assert lines[-1] == {"event": "job_finished", "state": "done", "result": DONE}
    assert not any("seq" in e or "at" in e for e in lines)  # the job's own dicts, unchanged
    _error(d.client.get("/v1/jobs/nope/events", headers=d.as_["cli"]), 404, "NOT_FOUND")


def test_failed_job_stream_ends_with_the_error_and_job_finished(settings, keys):
    gw = slow_history_gateway()
    gw.sandbox_public_token = lambda username="": (_ for _ in ()).throw(
        RuntimeError("secret-detail-4242")
    )
    d = _build(settings, keys, gw)
    lines = _job_stream(d, _start(d))
    assert lines[-2:] == [
        {
            "event": "error",
            "message": "Unexpected error in the daemon; see its log.",
            "error_code": "INTERNAL_ERROR",
        },
        {"event": "job_finished", "state": "failed", "result": None},
    ]
    assert "secret-detail-4242" not in json.dumps(lines + _published(d))


# ---------- daemon events from routes and stores ----------

SECOND_TOKEN = "access-sandbox-SECOND-456"


class TwoItemGateway(FakeGateway):
    """item_1 comes from the public-token exchange; item_2 is added straight to the database."""

    def __init__(self) -> None:
        super().__init__(_pages())
        self.second = {
            None: {
                "added": [txn("t9", account_id="acc_2")],
                "modified": [],
                "removed": [],
                "accounts": [account("acc_2")],
                "next_cursor": "d1",
                "has_more": False,
                "transactions_update_status": "INITIAL_UPDATE_COMPLETE",
            }
        }

    def transactions_sync(self, access_token, cursor, count=500):
        if access_token == SECOND_TOKEN:
            return self.second[cursor]
        return super().transactions_sync(access_token, cursor, count)


def test_sync_publishes_sync_completed_per_item(settings, keys):
    d = _build(settings, keys, TwoItemGateway())
    _exchange(d)
    with d.session_factory() as s:
        token = keys.cipher.encrypt(SECOND_TOKEN, token_context("item_2"))
        s.add(Item(id="item_2", institution_name="Second Bank", access_token_enc=token))
        s.commit()
    assert d.client.post("/v1/sync", headers=d.as_["cli"]).status_code == 200
    done = [e for e in _published(d) if e["event"] == "sync_completed"]
    assert {e["item_id"]: _fields(e) for e in done} == {
        "item_1": {
            "event": "sync_completed",
            "item_id": "item_1",
            "added": 2,
            "modified": 0,
            "removed": 0,
            "history_complete": True,
        },
        "item_2": {
            "event": "sync_completed",
            "item_id": "item_2",
            "added": 1,
            "modified": 0,
            "removed": 0,
            "history_complete": False,
        },
    }
    assert len(done) == 2
    assert {e["at"] for e in done} == {"2026-10-21T09:00:00Z"}  # the daemon's clock, UTC


def test_link_and_remove_publish_item_events(d):
    _exchange(d)
    assert _last(d, "item_linked") == {
        "event": "item_linked",
        "item_id": "item_1",
        "institution": "First Platypus Bank",
    }
    r = d.client.delete("/v1/items/item_1", headers=d.as_["cli"], params={"local_only": "true"})
    assert r.status_code == 204, r.text
    assert _last(d, "item_removed") == {
        "event": "item_removed",
        "item_id": "item_1",
        "institution": "First Platypus Bank",
    }


def test_grant_expired_is_published_by_a_timer_at_expiry(d):
    cli = d.as_["cli"]
    r = d.client.post("/v1/grants", headers=cli, json={"operations": ["sync"], "ttl_s": 600})
    assert r.status_code == 200, r.text
    grant = r.json()
    [timer] = d.timers.started
    assert timer.delay_s == 600  # armed for the expiry instant itself
    assert _last(d, "grant_created") == {"event": "grant_created", **grant}
    d.clock.advance(600)
    assert d.client.get("/v1/grants", headers=cli).json() is None  # expired (lazily)...
    assert _names(d) == ["grant_created"]  # ...and that check publishes nothing
    timer.fire()
    assert _names(d) == ["grant_created", "grant_expired"]
    assert _last(d, "grant_expired") == {"event": "grant_expired", **grant}
    timer.fire_late()  # a timer runs its expiry once
    assert _names(d) == ["grant_created", "grant_expired"]

    # A replaced or revoked grant's timer is cancelled and never reports an expiry.
    d.client.post("/v1/grants", headers=cli, json={"operations": ["sync"], "ttl_s": 60})
    second = d.client.post("/v1/grants", headers=cli, json={"operations": ["link"], "ttl_s": 120})
    replaced, current = d.timers.started[1:]
    assert replaced.cancelled and not current.cancelled
    assert d.client.delete("/v1/grants", headers=cli).status_code == 204
    assert current.cancelled
    assert _last(d, "grant_revoked") == {"event": "grant_revoked", **second.json()}
    replaced.fire_late()
    current.fire_late()
    assert d.client.delete("/v1/grants", headers=cli).status_code == 204  # nothing to revoke
    assert _names(d) == [
        "grant_created", "grant_expired", "grant_created", "grant_created", "grant_revoked",
    ]  # fmt: skip


def test_approval_requested_approved_and_expired_events(d):
    _exchange(d)
    d.grants.grant({"items.remove"}, 600)
    mcp = d.as_["mcp"]
    rid = _error(d.client.delete("/v1/items/item_1", headers=mcp), 403, "CONFIRMATION_REQUIRED")[
        "request_id"
    ]
    request_timer = d.timers.started[-1]
    _error(d.client.delete("/v1/items/item_1", headers=mcp), 403, "CONFIRMATION_REQUIRED")
    about = {
        "request_id": rid,
        "operation": "items.remove",
        "item_id": "item_1",
        "institution": "First Platypus Bank",
    }
    requested = [e for e in _published(d) if e["event"] == "approval_requested"]
    assert [_fields(e) for e in requested] == [{"event": "approval_requested", **about}]  # once
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["cli"]).status_code == 204
    assert _last(d, "approval_approved") == {"event": "approval_approved", **about}
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert r.status_code == 204, r.text
    assert _names(d)[-1] == "item_removed"
    assert request_timer.cancelled  # used up: it will never expire
    request_timer.fire_late()
    assert "approval_expired" not in _names(d)

    # A request nobody approves expires by its timer at the end of its 300 s.
    _exchange(d)
    rid2 = _error(d.client.delete("/v1/items/item_1", headers=mcp), 403, "CONFIRMATION_REQUIRED")[
        "request_id"
    ]
    timer = d.timers.started[-1]
    assert timer.delay_s == 300
    d.clock.advance(300)
    assert d.client.get("/v1/approvals", headers=d.as_["cli"]).json() == []  # gone lazily...
    assert "approval_expired" not in _names(d)  # ...without an event
    timer.fire()
    assert _last(d, "approval_expired") == {
        "event": "approval_expired",
        **about,
        "request_id": rid2,
    }


# ---------- the daemon-wide stream ----------


def test_after_seq_resumes_without_duplicates(d):
    for n in range(3):
        d.events.publish("test", n=n)
    cli = d.as_["cli"]
    with serve(d) as sock:
        with stream(sock, "/v1/events", cli, after=0) as s:
            assert s.response.headers["content-type"] == "application/x-ndjson"
            seen = [s.next() for _ in range(3)]  # published before the stream opened
            d.events.publish("test", n=3)  # and one while it is open
            seen.append(s.next())
        assert [(e["seq"], e["n"]) for e in seen] == [(1, 0), (2, 1), (3, 2), (4, 3)]
        assert seen[0] == {"seq": 1, "event": "test", "at": "2026-10-21T09:00:00Z", "n": 0}
        d.events.publish("test", n=4)
        d.events.publish("test", n=5)
        with stream(sock, "/v1/events", cli, after=seen[-1]["seq"]) as s:
            assert [s.next()["seq"], s.next()["seq"]] == [5, 6]
            d.events.publish("test", n=6)
            assert s.next()["seq"] == 7
        with stream(sock, "/v1/events", cli) as s:  # no `after`: from the moment it opens
            d.events.publish("test", n=7)
            assert s.next()["seq"] == 8


def test_evicted_seq_yields_a_gap_event(settings, keys):
    d = _build(settings, keys, FakeGateway(), events=EventBus(capacity=4, clock=FakeClock()))
    for n in range(6):
        d.events.publish("test", n=n)  # seq 1..6; the buffer keeps 3..6
    cli = d.as_["cli"]
    with serve(d) as sock:
        with stream(sock, "/v1/events", cli, after=1) as s:  # seq 2 is gone
            assert s.next() == {"event": "gap"}
            assert [s.next()["seq"] for _ in range(4)] == [3, 4, 5, 6]
        with stream(sock, "/v1/events", cli, after=2) as s:  # nothing after 2 is missing
            assert s.next()["seq"] == 3
        with stream(sock, "/v1/events", cli, after=99) as s:  # never issued: an older daemon's
            assert s.next() == {"event": "gap"}
            d.events.publish("test", n=6)
            assert s.next()["seq"] == 7


def test_idle_stream_sends_a_keepalive_line_after_silence(settings, keys):
    d = _build(settings, keys, FakeGateway(), keepalive_s=0.05)
    with serve(d) as sock, stream(sock, "/v1/events", d.as_["cli"]) as s:
        assert s.next() == {"event": "keepalive"}
        assert s.next() == {"event": "keepalive"}
        d.events.publish("test", n=1)
        line = s.next()
        while line == {"event": "keepalive"}:
            line = s.next()
        assert line["seq"] == 1


def test_too_many_streams_is_503(settings, keys, steps):
    async def fill_the_default_32() -> None:
        bus = EventBus()
        streams = [bus.open_stream(follow=True) for _ in range(32)]
        with pytest.raises(TooManyStreams):
            bus.open_stream(follow=False)
        bus.close_stream(streams[0])
        bus.open_stream(follow=False)

    asyncio.run(fill_the_default_32())

    d = _build(
        settings,
        keys,
        slow_history_gateway(),
        events=EventBus(max_subscribers=2, clock=FakeClock()),
        sleep=steps.sleep,
        clock=steps.clock.monotonic,
    )
    job_id = _start(d, interval=5)
    steps.wait_paused()
    cli = d.as_["cli"]
    with (
        serve(d) as sock,
        stream(sock, "/v1/events", cli) as events,
        stream(sock, f"/v1/jobs/{job_id}/events", cli) as job,  # job streams count too
    ):
        assert events.response.status_code == job.response.status_code == 200
        for path in ("/v1/events", f"/v1/jobs/{job_id}/events"):
            with stream(sock, path, cli) as third:
                _refused(third, 503, "TOO_MANY_STREAMS")
        steps.release()
        assert job.rest()[-1]["event"] == "job_finished"


def test_mcp_holds_at_most_8_streams_so_humans_can_still_follow(settings, keys, steps):
    async def fill_the_agents_8() -> None:
        bus = EventBus()
        agent = [bus.open_stream(follow=True, agent=True) for _ in range(8)]
        with pytest.raises(TooManyStreams) as refused:
            bus.open_stream(follow=True, agent=True)
        assert (refused.value.limit, refused.value.agent) == (8, True)
        humans = [bus.open_stream(follow=True) for _ in range(24)]  # the other 24 stay theirs
        assert len(agent) + len(humans) == 32
        bus.close_stream(agent[0])
        bus.open_stream(follow=False, agent=True)  # a freed agent slot is the agent's again

    asyncio.run(fill_the_agents_8())

    d = _build(
        settings, keys, slow_history_gateway(), sleep=steps.sleep, clock=steps.clock.monotonic
    )
    job_id = _start(d, interval=5)
    steps.wait_paused()
    mcp, cli = d.as_["mcp"], d.as_["cli"]
    job_path = f"/v1/jobs/{job_id}/events"
    with serve(d) as sock, contextlib.ExitStack() as open_streams:
        agent = [open_streams.enter_context(stream(sock, "/v1/events", mcp)) for _ in range(6)]
        agent += [open_streams.enter_context(stream(sock, job_path, mcp)) for _ in range(2)]
        assert all(s.response.status_code == 200 for s in agent)  # job streams count too
        for path in ("/v1/events", job_path):
            with stream(sock, path, mcp) as ninth:
                assert "agent (mcp)" in _refused(ninth, 503, "TOO_MANY_STREAMS")["detail"]
        with stream(sock, "/v1/events", cli) as events, stream(sock, job_path, cli) as job:
            assert events.response.status_code == job.response.status_code == 200
            steps.release()
            assert job.rest()[-1] == {
                "event": "job_finished",
                "state": "done",
                "result": _job(d, job_id)["result"],
            }


def test_the_33rd_stream_overall_is_refused(d):
    mcp, cli = d.as_["mcp"], d.as_["cli"]
    with serve(d) as sock, contextlib.ExitStack() as open_streams:
        held = [open_streams.enter_context(stream(sock, "/v1/events", mcp)) for _ in range(8)]
        held += [open_streams.enter_context(stream(sock, "/v1/events", cli)) for _ in range(24)]
        assert [s.response.status_code for s in held] == [200] * 32
        for role in (cli, mcp):
            with stream(sock, "/v1/events", role) as extra:
                detail = _refused(extra, 503, "TOO_MANY_STREAMS")["detail"]
                assert "32 event streams" in detail


def test_idle_streams_do_not_block_other_requests(d):
    async def few_threads() -> None:  # the server's thread pool shrinks to 4 threads
        anyio.to_thread.current_default_thread_limiter().total_tokens = 4

    d.app.add_api_route("/test/few-threads", few_threads, methods=["POST"])
    cli = d.as_["cli"]
    with serve(d) as sock, _http(sock) as http, contextlib.ExitStack() as open_streams:
        assert http.post("/test/few-threads").status_code == 200
        streams = [open_streams.enter_context(stream(sock, "/v1/events", cli)) for _ in range(8)]
        assert all(s.response.status_code == 200 for s in streams)  # twice the pool, all open
        started = time.monotonic()
        r = http.get("/v1/status", headers=cli)
        assert r.status_code == 200, r.text
        assert time.monotonic() - started < 2
        # Idle streams hold no database connection either (an audit write may still be ending).
        _eventually(lambda: d.engine.pool.checkedout() == 0, "pool connections to be returned")


def test_disconnect_frees_the_subscriber_slot(settings, keys):
    d = _build(settings, keys, FakeGateway(), events=EventBus(max_subscribers=1, clock=FakeClock()))
    cli = d.as_["cli"]
    with serve(d) as sock:
        with stream(sock, "/v1/events", cli) as first:
            assert first.response.status_code == 200
            with stream(sock, "/v1/events", cli) as second:
                _refused(second, 503, "TOO_MANY_STREAMS")
        # `first` hung up. The daemon notices at once (its keep-alive is 15 s away) and the
        # slot is free again.
        started = time.monotonic()

        def reopened() -> bool:
            with stream(sock, "/v1/events", cli) as again:
                return again.response.status_code == 200

        _eventually(reopened, "the slot to be freed")
        assert time.monotonic() - started < 5


@posix_only
def test_open_streams_end_when_the_daemon_stops():
    folder = Path(tempfile.mkdtemp(dir="/tmp", prefix="fs"))
    daemon = ThreadedDaemon(_settings(folder))
    try:
        daemon.start()
        loc = daemon.locations
        with stream(loc.socket, "/v1/events", {CRED: _token(loc)}) as s:
            assert s.response.status_code == 200
            started = time.monotonic()
            assert _shutdown(loc).status_code == 202
            assert s.rest() == []  # it ends instead of holding up the shutdown
        daemon.join()
        assert time.monotonic() - started < 10  # far less than the 30 s graceful wait
    finally:
        daemon.stop()
        shutil.rmtree(folder, ignore_errors=True)


# ---------- history jobs ----------


def test_history_job_waits_for_an_existing_item(d):
    _exchange(d)
    job_id = _start_history(d)
    lines = _job_stream(d, job_id)
    assert [e["event"] for e in lines] == [
        "waiting", "poll", "poll", "poll", "poll", "done", "job_finished",
    ]  # fmt: skip
    assert lines[0] == {"event": "waiting", "interval_s": 5.0, "timeout_s": 120.0}
    assert lines[-2] == {"event": "done", **DONE}
    assert lines[-1] == {"event": "job_finished", "state": "done", "result": DONE}
    about = {"job_id": job_id, "kind": "history", "item_id": "item_1"}
    assert _last(d, "job_started") == {"event": "job_started", **about}
    assert _last(d, "job_finished") == {
        "event": "job_finished",
        **about,
        "state": "done",
        "result": DONE,
    }
    passes = [e for e in _published(d) if e["event"] == "sync_completed"]
    assert [(e["item_id"], e["added"]) for e in passes] == [("item_1", n) for n in (0, 0, 2, 0)]

    body = {"item_id": "nope"}
    _error(d.client.post("/v1/jobs/history", headers=d.as_["cli"], json=body), 404, "NOT_FOUND")
    assert len(d.app.state.daemon.jobs._jobs) == 1  # no job for an unknown item


def test_history_job_409_names_the_running_job(settings, keys, steps):
    d = _build(
        settings, keys, slow_history_gateway(), sleep=steps.sleep, clock=steps.clock.monotonic
    )
    _exchange(d)
    _add_item(d, keys, "item_2")
    first = _start_history(d)
    steps.wait_paused()
    cli = d.as_["cli"]
    # One running job at a time, whatever its kind or Item: the 409 names the one to follow.
    for item_id in ("item_1", "item_2"):
        r = d.client.post("/v1/jobs/history", headers=cli, json={"item_id": item_id})
        assert _error(r, 409, "JOB_ALREADY_RUNNING")["job_id"] == first
    r = d.client.post("/v1/jobs/sandbox-link", headers=cli, json={})
    assert _error(r, 409, "JOB_ALREADY_RUNNING")["job_id"] == first
    assert len(d.app.state.daemon.jobs._jobs) == 1
    steps.release()
    assert _job_stream(d, first)[-1]["state"] == "done"
    assert _start_history(d, "item_2") != first  # once it has finished another may start


def test_history_job_while_a_sandbox_link_job_runs_is_409(settings, keys, steps):
    d = _build(
        settings, keys, slow_history_gateway(), sleep=steps.sleep, clock=steps.clock.monotonic
    )
    first = _start(d, interval=5)
    steps.wait_paused()  # it has linked item_1 and waits for its history
    r = d.client.post("/v1/jobs/history", headers=d.as_["cli"], json={"item_id": "item_1"})
    assert _error(r, 409, "JOB_ALREADY_RUNNING")["job_id"] == first
    assert len(d.app.state.daemon.jobs._jobs) == 1
    steps.release()
    assert _job_stream(d, first)[-1]["state"] == "done"


def test_history_job_needs_plaid_keys_before_any_job(settings, keys):
    settings.plaid_client_id = ""
    d = _build(settings, keys, None)
    with d.session_factory() as s:
        token = keys.cipher.encrypt(ACCESS_TOKEN, token_context("item_1"))
        s.add(Item(id="item_1", access_token_enc=token))
        s.commit()
    r = d.client.post("/v1/jobs/history", headers=d.as_["cli"], json={"item_id": "item_1"})
    _error(r, 503, "NOT_CONFIGURED")
    assert d.app.state.daemon.jobs._jobs == {}


def test_mcp_reads_events_but_needs_a_sync_grant_for_a_history_job(d):
    _exchange(d)
    mcp = d.as_["mcp"]
    body = {"item_id": "item_1"}
    _error(d.client.post("/v1/jobs/history", headers=mcp, json=body), 403, "WRITE_NOT_GRANTED")
    d.grants.grant({"link"}, 600)
    _error(d.client.post("/v1/jobs/history", headers=mcp, json=body), 403, "WRITE_NOT_GRANTED")
    d.grants.grant({"sync"}, 600)
    job_id = _start_history(d, role="mcp")
    assert _job_stream(d, job_id, role="mcp")[-1]["state"] == "done"  # reading needs `read`
    with serve(d) as sock, stream(sock, "/v1/events", mcp, after=0) as s:
        assert s.response.status_code == 200
        assert s.next()["event"] == "item_linked"
    _error(d.client.get("/v1/events"), 401, "UNAUTHENTICATED")
    _error(d.client.get(f"/v1/jobs/{job_id}/events"), 401, "UNAUTHENTICATED")


# ---------- what events may carry ----------

ALLOWED_FIELDS = {
    "seq", "event", "at", "item_id", "institution", "added", "modified", "removed",
    "history_complete", "job_id", "kind", "state", "result", "operations", "expires_at",
    "request_id", "operation",
}  # fmt: skip
EVENT_NAMES = {
    "sync_completed", "item_linked", "item_removed", "job_started", "job_finished",
    "grant_created", "grant_revoked", "grant_expired", "approval_requested",
    "approval_approved", "approval_expired",
}  # fmt: skip


def test_events_never_contain_transaction_names_or_tokens(settings, keys):
    d = _build(settings, keys, FakeGateway(_pages()))
    cli, mcp = d.as_["cli"], d.as_["mcp"]
    _exchange(d)
    assert d.client.post("/v1/sync", headers=cli).status_code == 200
    jobs = [_start(d, interval=5), _start_history(d)]
    job_lines = [line for job_id in jobs for line in _job_stream(d, job_id)]
    d.client.post("/v1/grants", headers=cli, json={"operations": ["sync"], "ttl_s": 60})
    d.client.delete("/v1/grants", headers=cli)
    d.client.post("/v1/grants", headers=cli, json={"operations": ["items.remove"], "ttl_s": 60})
    rid = d.client.delete("/v1/items/item_1", headers=mcp).json()["request_id"]
    d.client.post(f"/v1/approvals/{rid}", headers=cli)
    r = d.client.delete("/v1/items/item_1", headers=mcp, params={"request_id": rid})
    assert r.status_code == 204, r.text
    d.timers.started[-2].fire()  # the items.remove grant expires
    d.events.close()  # so the stream below ends once it has sent everything
    r = d.client.get("/v1/events", headers=cli, params={"after": 0})
    assert r.status_code == 200, r.text
    events = [json.loads(line) for line in r.text.splitlines()]

    assert {e["event"] for e in events} == EVENT_NAMES - {"approval_expired"}
    for e in events:
        assert set(e) <= ALLOWED_FIELDS, e
    wire = r.text + json.dumps(job_lines)
    secrets = [ACCESS_TOKEN, "public-sandbox-ok", "link-sandbox-abc", *d.tokens.values()]
    data = ["Txn t1", "Txn t2", "Starbucks", "987.65", "FOOD_AND_DRINK", "Plaid Checking"]
    for value in secrets + data:
        assert value not in wire, value
