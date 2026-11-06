"""`findata start --background`: a detached daemon that reports readiness through a pipe.

Real subprocesses on a short data dir under /tmp (Unix socket paths are limited to about 104
bytes). Only start, status and stop are used, so no Plaid call and no Touch ID prompt.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from findata import cli
from findata.client import DaemonClient
from findata.config import Settings
from findata.db import Base, make_engine
from findata.locations import Locations
from findata.security import load_keys

pytestmark = pytest.mark.skipif(os.name != "posix", reason="--background is POSIX-only")


@pytest.fixture
def data_dir():
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="fb"))
    yield path
    loc = Locations(path)
    try:  # never leak a daemon, whatever the test did
        pid = int(loc.pid.read_text())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.5)
    with contextlib.suppress(OSError, ValueError):  # a fake child must not outlive its test
        os.kill(int((path / "fake.pid").read_text()), signal.SIGKILL)
    shutil.rmtree(path, ignore_errors=True)


def _env(data_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "FINDATA_DATA_DIR": str(data_dir),
        "FINDATA_KEY_BACKEND": "file",
        "PLAID_CLIENT_ID": "",
        "PLAID_SECRET": "",
    }


def _findata(data_dir: Path, *args: str, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (our own interpreter and module)
        [sys.executable, "-m", "findata.cli", *args],
        cwd=data_dir,
        env=_env(data_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _pid_from(out: str) -> int:
    return int(out.split("(pid ")[1].split(")")[0])


def test_background_start_returns_when_the_daemon_is_ready(data_dir):
    started = _findata(data_dir, "start", "--background")
    assert started.returncode == 0, started.stderr
    pid = _pid_from(started.stdout)
    assert str(Locations(data_dir).log) in started.stdout
    assert _alive(pid)
    assert int(Locations(data_dir).pid.read_text()) == pid
    # Ready means serving: the very first request is answered.
    assert DaemonClient(data_dir, "cli", timeout=10).status().user_presence is not None
    assert _findata(data_dir, "stop").returncode == 0
    assert _wait_gone(pid)


def test_background_start_fails_loudly_if_the_child_dies(data_dir):
    settings = Settings(
        plaid_client_id="",
        plaid_secret="",
        findata_data_dir=data_dir,
        findata_key_backend="file",
        _env_file=None,
    )
    engine = make_engine(settings.db_path, load_keys(settings).db_key)
    legacy = [Base.metadata.tables[n] for n in ("items", "accounts", "transactions")]
    Base.metadata.create_all(engine, tables=legacy)  # schema behind: 0002 is pending
    engine.dispose()

    failed = _findata(data_dir, "start", "--background")

    assert failed.returncode == 1
    assert "findata migrate" in failed.stderr
    assert "Traceback" not in failed.stderr
    assert "started in the background" not in failed.stdout
    assert not _alive(int(failed.stderr.split("had pid ")[1].split(")")[0]))  # no stray process
    loc = Locations(data_dir)
    assert not loc.socket.exists() and not loc.pid.exists()


def test_background_daemon_logs_to_file_and_not_the_terminal(data_dir):
    started = _findata(data_dir, "start", "--background")
    try:
        assert started.returncode == 0, started.stderr
        assert "listening" not in started.stdout + started.stderr
        log = Locations(data_dir).log.read_text()
        assert "findata daemon" in log and "listening on" in log
        assert "findata stop" in log and "Ctrl+C" not in log
    finally:
        _findata(data_dir, "stop")


def test_status_and_stop_work_on_a_backgrounded_daemon(data_dir):
    started = _findata(data_dir, "start", "--background")
    assert started.returncode == 0, started.stderr
    pid = _pid_from(started.stdout)
    status = _findata(data_dir, "status")
    assert status.returncode == 0 and "schema" in status.stdout
    stopped = _findata(data_dir, "stop")
    assert stopped.returncode == 0
    assert _wait_gone(pid)
    assert _findata(data_dir, "status").returncode == 1  # nothing running any more
    assert not Locations(data_dir).socket.exists()


def test_log_file_is_owner_only_and_contains_no_credentials(data_dir):
    started = _findata(data_dir, "start", "--background")
    try:
        assert started.returncode == 0, started.stderr
        loc = Locations(data_dir)
        assert stat.S_IMODE(loc.log.stat().st_mode) == 0o600
        _findata(data_dir, "status")
    finally:
        _findata(data_dir, "stop")
    log = loc.log.read_text()
    for token in loc.credentials_dir.glob("*.token"):
        assert token.read_text().strip() not in log
    assert "--ready-fd" not in log and "ready\n" not in log.replace("not ready", "")


def test_a_group_readable_log_is_refused(data_dir):
    loc = Locations(data_dir)
    loc.log.write_text("old\n")
    loc.log.chmod(0o640)
    failed = _findata(data_dir, "start", "--background")
    assert failed.returncode == 1
    assert "chmod 600" in failed.stderr
    assert not loc.socket.exists()


FAKE_CHILD = textwrap.dedent(
    """
    import os, socket, sys, time
    from pathlib import Path
    fd, data, delay, mode = int(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    Path(data, "fake.pid").write_text(str(os.getpid()))
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(data / "daemon.sock"))
    sock.listen()
    sock.settimeout(0.05)
    deadline = time.monotonic() + delay
    connections = 0
    while time.monotonic() < deadline:
        try:
            sock.accept()
            connections += 1
        except socket.timeout:
            pass
    Path(data, "fake.connections").write_text(str(connections))
    if mode == "ready":
        os.write(fd, b"ready\\n")
        os.close(fd)
    time.sleep(60)
    """
)


def _fake_child(monkeypatch, data_dir: Path, delay: float, mode: str) -> None:
    from findata.cli import background

    script = data_dir / "fake_child.py"
    script.write_text(FAKE_CHILD)
    monkeypatch.setattr(
        background,
        "child_command",
        lambda fd: [sys.executable, str(script), str(fd), str(data_dir), str(delay), mode],
    )
    monkeypatch.setenv("FINDATA_DATA_DIR", str(data_dir))


def test_background_start_waits_on_the_ready_signal(data_dir, monkeypatch):
    """The parent returns only when the child says so, and makes no request to the child's
    socket while it waits (the fake child counts connections)."""
    _fake_child(monkeypatch, data_dir, delay=1.5, mode="ready")
    began = time.monotonic()
    result = CliRunner().invoke(cli.app, ["start", "--background", "--wait-timeout", "20"])
    elapsed = time.monotonic() - began
    assert result.exit_code == 0, result.output
    assert elapsed >= 1.5
    assert (data_dir / "fake.connections").read_text() == "0"
    pid = int((data_dir / "fake.pid").read_text())
    assert f"pid {pid}" in result.output


def test_background_start_times_out_and_leaves_no_process(data_dir, monkeypatch):
    _fake_child(monkeypatch, data_dir, delay=0.1, mode="never")
    result = CliRunner().invoke(cli.app, ["start", "--background", "--wait-timeout", "1"])
    assert result.exit_code == 1
    assert "not ready" in result.output
    assert not _alive(int((data_dir / "fake.pid").read_text()))


def test_a_daemon_whose_parent_went_away_keeps_serving(data_dir):
    """The ready write hits a closed pipe (terminal closed mid-start): not fatal."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module)
        [sys.executable, "-m", "findata.cli", "start", "--ready-fd", str(write_fd)],
        cwd=data_dir,
        env=_env(data_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, "the daemon exited instead of serving"
            try:
                DaemonClient(data_dir, "cli", timeout=5).status()
                break
            except Exception:
                assert time.monotonic() < deadline
                time.sleep(0.1)
        time.sleep(0.5)
        assert proc.poll() is None
        assert _findata(data_dir, "stop").returncode == 0
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_background_start_while_a_daemon_runs_names_that_daemon(data_dir):
    first = _findata(data_dir, "start", "--background")
    try:
        assert first.returncode == 0, first.stderr
        running = _pid_from(first.stdout)
        second = _findata(data_dir, "start", "--background")
        assert second.returncode == 1
        assert second.stderr.startswith("Error: ")
        assert (
            f"already running for this data directory (pid {running})"
            in second.stderr.splitlines()[0]
        )
        assert _alive(running)
        assert int(Locations(data_dir).pid.read_text()) == running
    finally:
        _findata(data_dir, "stop")


def test_background_is_refused_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(cli.app, ["start", "--background"])
    assert result.exit_code == 1
    assert "not supported on Windows yet" in result.output
    assert "findata start" in result.output


def test_wait_timeout_without_background_is_refused():
    result = CliRunner().invoke(cli.app, ["start", "--wait-timeout", "5"])
    assert result.exit_code == 1
    assert "--background" in result.output


def test_the_ready_fd_is_not_inheritable():
    from findata.daemon.server import ReadySignal

    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(write_fd, True)
        signal_ = ReadySignal(write_fd)
        assert not os.get_inheritable(write_fd)
        signal_.close()
    finally:
        os.close(read_fd)
