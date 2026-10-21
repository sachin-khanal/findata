"""`findata start`: the single-instance lock, the owner-only socket, shutdown, the scheduler.

Anything that binds a Unix socket uses a short data dir under /tmp: socket paths are limited
to about 104 bytes and pytest's tmp_path is longer than that on macOS.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
import uvicorn
from typer.testing import CliRunner

from findata import cli
from findata.config import Settings
from findata.daemon.lock import DaemonAlreadyRunning, DaemonLock
from findata.daemon.scheduler import Scheduler
from findata.daemon.server import open_listener, run_daemon, use_tcp_fallback
from findata.db import Base, make_engine
from findata.locations import Locations
from findata.security import load_keys

CRED = "X-Findata-Credential"
ROLES = ("web", "cli", "mcp")
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals")


def _settings(data_dir: Path) -> Settings:
    # No Plaid keys: the daemon must start without them.
    return Settings(
        plaid_client_id="",
        plaid_secret="",
        findata_data_dir=data_dir,
        findata_key_backend="file",  # never touch the developer's real OS keychain in tests
        _env_file=None,
    )


@pytest.fixture
def data_dir():
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="fd"))  # owner-only (0700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _client(locations: Locations) -> httpx2.Client:
    transport = httpx2.HTTPTransport(uds=str(locations.socket))
    return httpx2.Client(transport=transport, base_url="http://daemon", timeout=10)


def _token(locations: Locations, role: str = "cli") -> str:
    return (locations.credentials_dir / f"{role}.token").read_text().strip()


def _status(locations: Locations) -> httpx2.Response:
    with _client(locations) as client:
        return client.get("/v1/status", headers={CRED: _token(locations)})


def _shutdown(locations: Locations) -> httpx2.Response:
    with _client(locations) as client:
        return client.post("/v1/shutdown", headers={CRED: _token(locations)})


def _wait_for(condition: Callable[[], bool], timeout: float = 20.0, what: str = "it") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _wait_until_ready(locations: Locations, alive: Callable[[], bool]) -> None:
    def ready() -> bool:
        if not alive():
            raise AssertionError("the daemon exited before it was ready")
        try:
            return _status(locations).status_code == 200
        except (httpx2.TransportError, OSError):  # no socket or credentials yet
            return False

    _wait_for(ready, what="the daemon to answer /v1/status")


class ThreadedDaemon:
    """`run_daemon` in a background thread of this process (so its pid is ours)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.locations = Locations(settings.data_dir)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            run_daemon(self.settings)
        except BaseException as exc:
            self.error = exc

    def start(self) -> None:
        self.thread.start()
        _wait_until_ready(self.locations, self.thread.is_alive)

    def join(self) -> None:
        self.thread.join(timeout=20)
        assert not self.thread.is_alive(), "the daemon did not stop"

    def stop(self) -> None:
        if self.thread.is_alive():
            with contextlib.suppress(httpx2.TransportError):
                _shutdown(self.locations)
            self.join()


@pytest.fixture
def daemon(data_dir):
    handle = ThreadedDaemon(_settings(data_dir))
    yield handle
    handle.stop()


def _invoke_start(settings: Settings, monkeypatch):
    monkeypatch.setattr("findata.config.get_settings", lambda: settings)
    return CliRunner().invoke(cli.app, ["start"])


def _assert_lock_is_free(locations: Locations) -> None:
    with DaemonLock(locations.lock, locations.pid):
        pass


def _spawn_start(data_dir: Path) -> subprocess.Popen:
    """A real `findata start` process with its own environment and no .env in its cwd."""
    env = {
        **os.environ,
        "FINDATA_DATA_DIR": str(data_dir),
        "FINDATA_KEY_BACKEND": "file",
        "PLAID_CLIENT_ID": "",
        "PLAID_SECRET": "",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "findata.cli", "start"],
        cwd=data_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.fixture
def spawned():
    procs: list[subprocess.Popen] = []

    def spawn(data_dir: Path) -> subprocess.Popen:
        proc = _spawn_start(data_dir)
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:  # never leak a daemon, whatever the test did
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


# ---------- the single-instance lock ----------


def test_lock_is_exclusive_and_records_the_holders_pid(data_dir):
    loc = Locations(data_dir)
    held = DaemonLock(loc.lock, loc.pid)
    held.acquire()
    try:
        assert int(loc.pid.read_text()) == os.getpid()
        with pytest.raises(DaemonAlreadyRunning) as refused:
            DaemonLock(loc.lock, loc.pid).acquire()
        assert refused.value.pid == os.getpid()
        assert f"pid {os.getpid()}" in str(refused.value)
    finally:
        held.release()
    assert not loc.pid.exists()
    _assert_lock_is_free(loc)  # released, and usable as a context manager


def test_second_start_is_refused_with_the_running_pid(daemon, monkeypatch):
    daemon.start()
    r = _invoke_start(daemon.settings, monkeypatch)
    assert r.exit_code == 1
    assert f"pid {os.getpid()}" in r.output
    assert "Traceback" not in r.output
    # The refused start left the running daemon's socket alone.
    assert _status(daemon.locations).status_code == 200


CRASHING_DAEMON = """
import socket, sys, time
from pathlib import Path
from findata.daemon.lock import DaemonLock
lock, pid, sock_path = map(Path, sys.argv[1:4])
DaemonLock(lock, pid).acquire()
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.bind(str(sock_path))
sock.listen()
print("ready", flush=True)
time.sleep(60)
"""


@posix_only
def test_stale_socket_and_lock_after_a_crash_do_not_block_start(daemon):
    loc = daemon.locations
    args = [str(loc.lock), str(loc.pid), str(loc.socket)]
    child = subprocess.Popen(  # noqa: S603 (our own interpreter and script)
        [sys.executable, "-c", CRASHING_DAEMON, *args],
        cwd=loc.data_dir,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(DaemonAlreadyRunning) as refused:  # really held while it lives
            DaemonLock(loc.lock, loc.pid).acquire()
        assert refused.value.pid == child.pid
    finally:
        child.kill()  # SIGKILL: no cleanup code runs
        child.communicate(timeout=10)
    assert child.returncode == -signal.SIGKILL
    # What a crash leaves behind: the lock file, a pid file naming a dead process, a socket.
    assert loc.lock.exists() and stat.S_ISSOCK(loc.socket.stat().st_mode)
    assert int(loc.pid.read_text()) == child.pid

    daemon.start()  # becomes ready: not refused, and the stale socket was replaced

    assert int(loc.pid.read_text()) == os.getpid()
    assert _status(loc).status_code == 200


# ---------- startup failures ----------


def _create_pre_migration_tables(settings: Settings) -> None:
    """A database from before migrations: adopted as 0001, so 0002 is pending."""
    engine = make_engine(settings.db_path, load_keys(settings).db_key)
    legacy = [Base.metadata.tables[name] for name in ("items", "accounts", "transactions")]
    Base.metadata.create_all(engine, tables=legacy)
    engine.dispose()


def test_start_refuses_when_schema_is_behind(data_dir, monkeypatch):
    settings = _settings(data_dir)
    _create_pre_migration_tables(settings)
    r = _invoke_start(settings, monkeypatch)
    assert r.exit_code == 1
    assert "findata migrate" in r.output
    assert "Traceback" not in r.output
    loc = Locations(data_dir)
    assert not loc.socket.exists() and not loc.pid.exists()
    _assert_lock_is_free(loc)


def test_start_without_the_master_key_fails_cleanly(data_dir, monkeypatch):
    settings = _settings(data_dir)
    settings.db_path.write_bytes(b"\0" * 64)  # a database whose key is gone
    r = _invoke_start(settings, monkeypatch)
    assert r.exit_code == 1
    assert "master key" in r.output
    assert "Traceback" not in r.output
    _assert_lock_is_free(Locations(data_dir))


def test_too_long_socket_path_fails_with_a_clear_message(tmp_path, monkeypatch):
    long_dir = tmp_path / ("d" * 120)
    long_dir.mkdir(mode=0o700)
    r = _invoke_start(_settings(long_dir), monkeypatch)
    assert r.exit_code == 1
    assert str(Locations(long_dir).socket) in r.output
    assert "FINDATA_DATA_DIR" in r.output
    assert "Traceback" not in r.output


# ---------- the socket ----------


@posix_only
def test_socket_is_owner_only(daemon):
    daemon.start()
    mode = daemon.locations.socket.stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o600


@posix_only
def test_socket_is_owner_only_from_the_moment_it_is_bound(data_dir, monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)  # no tightening afterwards
    loc = Locations(data_dir)
    caller_umask = os.umask(0)  # the loosest umask a user could have
    try:
        sock = open_listener(loc, use_tcp=False)
        umask_after = os.umask(caller_umask)
    finally:
        os.umask(caller_umask)
    try:
        assert sock.family == socket.AF_UNIX
        assert stat.S_IMODE(loc.socket.stat().st_mode) == 0o600
        assert umask_after == 0  # the caller's umask is restored
    finally:
        sock.close()


def test_socket_is_removed_on_clean_shutdown(daemon):
    daemon.start()
    assert _shutdown(daemon.locations).status_code == 202
    daemon.join()
    assert daemon.error is None
    loc = daemon.locations
    assert not loc.socket.exists() and not loc.pid.exists()
    _assert_lock_is_free(loc)


def test_socket_and_lock_are_released_when_serving_fails(data_dir, monkeypatch):
    def boom(self, sockets=None):
        raise RuntimeError("the server broke")

    monkeypatch.setattr(uvicorn.Server, "run", boom)
    with pytest.raises(RuntimeError, match="the server broke"):
        run_daemon(_settings(data_dir))
    loc = Locations(data_dir)
    assert not loc.socket.exists() and not loc.pid.exists()
    _assert_lock_is_free(loc)


def test_windows_listens_on_loopback_and_records_the_port(data_dir):
    assert use_tcp_fallback("nt") and not use_tcp_fallback("posix")
    loc = Locations(data_dir)
    sock = open_listener(loc, use_tcp=True)
    try:
        host, port = sock.getsockname()[:2]
        assert (sock.family, host) == (socket.AF_INET, "127.0.0.1")
        assert int(loc.port_file.read_text()) == port
        if os.name == "posix":
            assert stat.S_IMODE(loc.port_file.stat().st_mode) == 0o600
        assert not loc.socket.exists()
    finally:
        sock.close()


# ---------- a real `findata start` process ----------


@posix_only
def test_stop_shuts_the_daemon_down(data_dir, spawned):
    loc = Locations(data_dir)
    proc = spawned(data_dir)
    _wait_until_ready(loc, lambda: proc.poll() is None)
    tokens = [_token(loc, role) for role in ROLES]
    assert _shutdown(loc).status_code == 202
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert not loc.socket.exists() and not loc.pid.exists()
    _assert_lock_is_free(loc)
    assert "/v1/" not in out  # no access log: the audit log is the record of requests
    assert not any(token in out for token in tokens)


@posix_only
def test_sigterm_stops_the_daemon_cleanly(data_dir, spawned):
    loc = Locations(data_dir)
    proc = spawned(data_dir)
    _wait_until_ready(loc, lambda: proc.poll() is None)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert not loc.socket.exists() and not loc.pid.exists()


def test_start_command_module_imports_no_core_modules_at_module_level():
    """Ruling R7: the command module loads core code only when `start` actually runs.

    (Importing it through `findata.cli` still loads core via the other command modules
    until they are made lazy too, so this checks the module's own top-level imports.)
    """
    path = Path(cli.__file__).parent / "commands" / "daemon.py"
    tree = ast.parse(path.read_text())
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    allowed = {"__future__", "typer", "findata.cli.output"}
    assert set(top_level) <= allowed, top_level


# ---------- the scheduler ----------


def test_scheduler_runs_registered_job_and_stops():
    runs: list[float] = []
    scheduler = Scheduler()
    scheduler.register("tick", 0.01, lambda: runs.append(time.monotonic()))
    scheduler.start()
    _wait_for(lambda: len(runs) >= 3, timeout=5, what="three runs")
    began = time.monotonic()
    scheduler.stop()
    assert time.monotonic() - began < 1  # stop() is prompt
    after_stop = len(runs)
    time.sleep(0.1)
    assert len(runs) == after_stop


def test_failing_job_is_logged_by_type_only_and_keeps_its_schedule(caplog):
    calls: list[int] = []

    def job() -> None:
        calls.append(1)
        raise ValueError("merchant=Starbucks amount=12.50")

    scheduler = Scheduler()
    scheduler.register("sync", 0.01, job)
    with caplog.at_level(logging.WARNING, logger="findata.daemon.scheduler"):
        scheduler.start()
        _wait_for(lambda: len(calls) >= 2, timeout=5, what="two runs")
        scheduler.stop()
    assert "sync" in caplog.text and "ValueError" in caplog.text
    assert "Starbucks" not in caplog.text


def test_scheduler_with_no_jobs_starts_and_stops():
    scheduler = Scheduler()
    scheduler.start()
    scheduler.stop()
