"""`findata start --background`: spawn the daemon detached and wait for it to say it is ready.

Readiness comes from the child, never from probing it: the parent hands the child the write
end of a pipe (`--ready-fd`); the daemon writes one line, `ready`, once it is serving, and
closes it. End-of-file before that line means the child died. Imports only the standard
library and owner-only file helpers, so a front end never loads key or database code.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from findata.privfs import create_private_file

READY_LINE = b"ready\n"
DEFAULT_WAIT_S = 30.0
LOG_TAIL_LINES = 20
STOP_GRACE_S = 10.0


class BackgroundStartError(RuntimeError):
    """The daemon did not come up; the message says why and quotes the last log lines."""


@dataclass(frozen=True)
class Started:
    pid: int
    log: Path


def child_command(ready_fd: int) -> list[str]:
    return [sys.executable, "-m", "findata.cli", "start", "--ready-fd", str(ready_fd)]


def _open_log(log: Path):
    create_private_file(log)  # 0600 from the first byte; refuses a group/other-accessible log
    return log.open("ab")


def _log_tail(log: Path, offset: int) -> str:
    try:
        with log.open("rb") as fh:
            fh.seek(offset)
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _wait_for_ready(read_fd: int, timeout: float) -> bool:
    """True once the ready line arrives; False at end-of-file; TimeoutError if neither."""
    deadline = time.monotonic() + timeout
    data = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        readable, _, _ = select.select([read_fd], [], [], remaining)
        if not readable:
            raise TimeoutError
        chunk = os.read(read_fd, 64)
        if not chunk:
            return False
        data += chunk
        if data.startswith(READY_LINE):
            return True


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def start_in_background(log: Path, timeout: float = DEFAULT_WAIT_S) -> Started:
    """Spawn the daemon (new session, no terminal, output appended to `log`) and block until
    it reports ready. Raises BackgroundStartError, leaving no process behind."""
    log_fh = _open_log(log)
    offset = log.stat().st_size
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module)
            child_command(write_fd),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            pass_fds=(write_fd,),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            close_fds=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)  # only the child holds it now, so its exit shows up as EOF
        log_fh.close()
    try:
        try:
            ready = _wait_for_ready(read_fd, timeout)
        except TimeoutError:
            _terminate(proc)
            raise BackgroundStartError(
                _failure(
                    f"The daemon was not ready after {timeout:g}s and was stopped.",
                    proc,
                    log,
                    offset,
                )
            ) from None
        except BaseException:
            _terminate(proc)  # Ctrl+C while waiting: leave nothing half-started
            raise
        if not ready:
            try:
                code = proc.wait(timeout=5)
                why = f"The daemon exited (code {code}) before it was ready."
            except subprocess.TimeoutExpired:
                _terminate(proc)
                why = "The daemon closed its ready signal without being ready and was stopped."
            raise BackgroundStartError(_failure(why, proc, log, offset))
    finally:
        os.close(read_fd)
    return Started(proc.pid, log)


def _failure(why: str, proc: subprocess.Popen, log: Path, offset: int) -> str:
    tail = _log_tail(log, offset)
    detail = f"\nLast log lines ({log}):\n{tail}" if tail else f"\nLog: {log}"
    return f"{why} (pid {proc.pid}){detail}"
