"""Sandbox-link jobs in the daemon: same event stream the CLI emitted, polled over /v1/jobs."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from findata.daemon.app import create_daemon_app
from findata.daemon.audit import AuditLog
from findata.daemon.auth import CredentialStore
from findata.daemon.deps import PLAID_WRITE_LOCK
from findata.daemon.grants import ApprovalStore, GrantStore
from findata.daemon.jobs import JobManager
from findata.db import make_engine, make_session_factory
from findata.locations import Locations
from findata.plaid_gateway import PlaidError
from findata.runtime import Runtime
from findata.schema import ensure_current
from findata.sync import HISTORY_COMPLETE
from findata.wire import SandboxLinkIn
from tests.conftest import FakeGateway, FakePresence
from tests.test_daemon_api import CRED, ROLES, FakeClock, _error
from tests.test_wait_and_cli import Clock, page, slow_history_gateway

URL = "/v1/jobs/sandbox-link"


def _build(settings, keys, gateway, sleep=None, clock=None):
    engine = make_engine(settings.db_path, keys.db_key)
    ensure_current(engine)
    runtime = Runtime(settings, make_session_factory(engine), keys.cipher, gateway)
    locations = Locations(settings.data_dir)
    credentials = CredentialStore(locations)
    credentials.ensure_all()
    fake = FakeClock()
    grants = GrantStore(fake)
    app = create_daemon_app(
        runtime,
        credentials,
        grants,
        ApprovalStore(fake),
        AuditLog(runtime.session_factory, fake),
        started_at=fake(),
        clock=fake,
        presence=FakePresence("confirmed"),
        job_sleep=sleep,
        job_clock=clock,
    )
    tokens = {r: (locations.credentials_dir / f"{r}.token").read_text().strip() for r in ROLES}
    return SimpleNamespace(
        app=app,
        client=TestClient(app, base_url="http://127.0.0.1"),
        as_={r: {CRED: t} for r, t in tokens.items()},
        grants=grants,
    )


def _start(d, role="cli", **body) -> str:
    r = d.client.post(URL, headers=d.as_[role], json=body)
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"job_id"}
    return r.json()["job_id"]


def _finish(d, job_id, role="cli") -> dict:
    for _ in range(500):
        job = d.client.get(f"/v1/jobs/{job_id}", headers=d.as_[role]).json()
        if job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def d(settings, keys, clock):
    return _build(settings, keys, slow_history_gateway(), clock.sleep, clock.monotonic)


def test_job_emits_the_same_event_sequence_as_the_old_cli(d):
    job = _finish(d, _start(d, interval=5))
    assert job["state"] == "done"
    assert [e["event"] for e in job["events"]] == [
        "linking", "linked", "waiting", "poll", "poll", "poll", "poll", "done",
    ]  # fmt: skip
    done = {
        "event": "done",
        "added": 2,
        "status": HISTORY_COMPLETE,
        "history_complete": True,
        "timed_out": False,
        "polls": 4,
    }
    assert job["events"][-1] == done
    assert job["result"] == {k: v for k, v in done.items() if k != "event"}
    assert job["events"][0] == {"event": "linking", "username": "user_transactions_dynamic"}


def test_second_job_reuses_the_item_and_no_wait_emits_single_done(d):
    _finish(d, _start(d, interval=5))
    job = _finish(d, _start(d, wait=False))
    assert [e["event"] for e in job["events"]] == ["reused", "done"]
    assert job["events"][-1]["timed_out"] is False and "polls" not in job["events"][-1]
    assert job["result"]["timed_out"] is False


def test_timeout_job_finishes_with_timed_out_true(settings, keys, clock):
    gw = FakeGateway(
        {
            None: page("c1", "INITIAL_UPDATE_COMPLETE"),
            **{f"c{i}": page(f"c{i + 1}", "INITIAL_UPDATE_COMPLETE") for i in range(1, 30)},
        }
    )
    d = _build(settings, keys, gw, clock.sleep, clock.monotonic)
    job = _finish(d, _start(d, timeout=10, interval=5))
    assert job["state"] == "done"
    assert job["events"][-1]["history_complete"] is False
    assert job["events"][-1]["timed_out"] is True
    assert job["result"]["history_complete"] is False and job["result"]["timed_out"] is True


def test_plaid_error_fails_the_job_with_error_code(settings, keys, clock):
    gw = slow_history_gateway()
    gw.sandbox_public_token = lambda username="": (_ for _ in ()).throw(
        PlaidError("INVALID_API_KEYS", "invalid client_id or secret provided", 400)
    )
    d = _build(settings, keys, gw, clock.sleep, clock.monotonic)
    job = _finish(d, _start(d))
    assert job["state"] == "failed"
    assert job["events"][-1] == {
        "event": "error",
        "message": "Plaid rejected the request (INVALID_API_KEYS): "
        "INVALID_API_KEYS: invalid client_id or secret provided",
        "error_code": "INVALID_API_KEYS",
    }


def test_unexpected_error_is_generic_and_leaks_nothing(settings, keys, clock):
    gw = slow_history_gateway()
    gw.sandbox_public_token = lambda username="": (_ for _ in ()).throw(
        RuntimeError("secret-detail-4242")
    )
    d = _build(settings, keys, gw, clock.sleep, clock.monotonic)
    r = d.client.post(URL, headers=d.as_["cli"], json={})
    job = _finish(d, r.json()["job_id"])
    assert job["state"] == "failed"
    assert job["events"][-1] == {
        "event": "error",
        "message": "Unexpected error in the daemon; see its log.",
        "error_code": "INTERNAL_ERROR",
    }
    assert "secret-detail-4242" not in str(job)


def test_events_can_be_read_while_running(settings, keys):
    proceed, reached = threading.Event(), threading.Event()
    gw = slow_history_gateway()
    clock = Clock()

    def gated_sleep(seconds):
        reached.set()
        assert proceed.wait(10)
        clock.sleep(seconds)

    d = _build(settings, keys, gw, gated_sleep, clock.monotonic)
    job_id = _start(d, interval=5)
    assert reached.wait(10)
    mid = d.client.get(f"/v1/jobs/{job_id}", headers=d.as_["cli"]).json()
    assert mid["state"] == "running" and mid["result"] is None
    assert [e["event"] for e in mid["events"]][:4] == ["linking", "linked", "waiting", "poll"]
    proceed.set()
    assert _finish(d, job_id)["state"] == "done"


def test_job_waits_for_the_process_wide_write_lock(d):
    with PLAID_WRITE_LOCK:
        job_id = _start(d, interval=5)
        time.sleep(0.1)
        job = d.client.get(f"/v1/jobs/{job_id}", headers=d.as_["cli"]).json()
        assert job["state"] == "running" and job["events"] == []
    assert _finish(d, job_id)["state"] == "done"


def test_mcp_needs_both_link_and_sync_grants_to_start_a_job(d):
    mcp = d.as_["mcp"]
    _error(d.client.post(URL, headers=mcp, json={}), 403, "WRITE_NOT_GRANTED")
    d.grants.grant({"link"}, 600)
    _error(d.client.post(URL, headers=mcp, json={}), 403, "WRITE_NOT_GRANTED")
    d.grants.grant({"link", "sync"}, 600)
    job_id = _start(d, role="mcp")
    assert _finish(d, job_id, role="mcp")["state"] == "done"


def test_reading_a_job_needs_only_read(d):
    job_id = _start(d)
    _finish(d, job_id)
    r = d.client.get(f"/v1/jobs/{job_id}", headers=d.as_["mcp"])  # no grant at all
    assert r.status_code == 200 and r.json()["state"] == "done"


def test_missing_plaid_keys_is_503_and_creates_no_job(settings, keys):
    settings.plaid_client_id = ""
    d = _build(settings, keys, None)
    _error(d.client.post(URL, headers=d.as_["cli"], json={}), 503, "NOT_CONFIGURED")
    assert d.app.state.daemon.jobs._jobs == {}


def test_sync_can_complete_between_two_polls_of_a_running_job(settings, keys):
    paused, proceed = threading.Event(), threading.Event()
    clock = Clock()

    def gated_sleep(seconds):
        paused.set()
        assert proceed.wait(10)
        clock.sleep(seconds)

    d = _build(settings, keys, slow_history_gateway(), gated_sleep, clock.monotonic)
    job_id = _start(d, interval=5)
    assert paused.wait(10)  # the job is between two polls, sleeping
    assert PLAID_WRITE_LOCK.acquire(blocking=False), "lock must be free during the sleep"
    PLAID_WRITE_LOCK.release()
    r = d.client.post("/v1/sync", headers=d.as_["cli"])
    assert r.status_code == 200, r.text
    proceed.set()
    assert _finish(d, job_id)["state"] == "done"


def _gated(settings, keys):
    release, started = threading.Event(), threading.Event()
    clock = Clock()

    def gated_sleep(seconds):
        started.set()
        assert release.wait(10)
        clock.sleep(seconds)

    return _build(settings, keys, slow_history_gateway(), gated_sleep, clock.monotonic), (
        release,
        started,
    )


def test_second_job_while_one_runs_is_409_with_the_running_id(settings, keys):
    d, (release, started) = _gated(settings, keys)
    first = _start(d, interval=5)
    assert started.wait(10)
    body = _error(d.client.post(URL, headers=d.as_["cli"], json={}), 409, "JOB_ALREADY_RUNNING")
    assert body["job_id"] == first
    assert f"/v1/jobs/{first}/events" in body["detail"] and "{job_id}" not in body["detail"]
    assert len(d.app.state.daemon.jobs._jobs) == 1
    release.set()
    assert _finish(d, first)["state"] == "done"
    assert _finish(d, _start(d, interval=5))["state"] == "done"


def test_two_simultaneous_posts_start_exactly_one_job(settings, keys):
    d, (release, _) = _gated(settings, keys)  # the first job stays running during both posts
    barrier = threading.Barrier(2)
    codes = []

    def post():
        barrier.wait()
        codes.append(d.client.post(URL, headers=d.as_["cli"], json={"interval": 5}).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    release.set()
    assert sorted(codes) == [202, 409]


class _Boom(BaseException):
    pass


def test_base_exception_fails_the_job_and_releases_the_slot():
    jm = JobManager()

    def boom(emit):
        raise _Boom("secret-detail-9")

    job_id = jm.start(boom, exclusive="sandbox-link")
    for _ in range(500):
        if jm.get(job_id).state != "running":
            break
        time.sleep(0.01)
    job = jm.get(job_id)
    assert job.state == "failed"
    assert job.events[-1]["error_code"] == "INTERNAL_ERROR"
    assert "secret-detail-9" not in str(job)
    assert jm.get(jm.start(lambda emit: {}, exclusive="sandbox-link")) is not None


def test_unknown_job_id_is_404(d):
    _error(d.client.get("/v1/jobs/nope", headers=d.as_["cli"]), 404, "NOT_FOUND")


def test_bounds_and_wire_defaults(d):
    body = SandboxLinkIn()
    assert (body.username, body.interval, body.timeout, body.wait, body.new) == (
        None, 5.0, 120.0, True, False,
    )  # fmt: skip
    for bad in ({"timeout": -1}, {"timeout": 3601}, {"interval": 0.4}, {"interval": 61}):
        _error(d.client.post(URL, headers=d.as_["cli"], json=bad), 422, "VALIDATION_ERROR")


def test_manager_evicts_oldest_finished_job_beyond_the_bound():
    jm = JobManager(max_jobs=3)
    ids = []
    for i in range(5):
        ids.append(jm.start(lambda emit, i=i: {"i": i}))
        for _ in range(500):
            if jm.get(ids[-1]).state != "running":
                break
            time.sleep(0.01)
    with pytest.raises(KeyError):
        jm.get(ids[0])
    assert jm.get(ids[-1]).result == {"i": 4}
