"""Single-instance lock: at most one daemon per data directory.

An OS advisory lock on `daemon.lock` (`flock` on POSIX, `msvcrt.locking` on Windows). The OS
drops it when the holder's process ends, however it ends (`kill -9` included), so a crashed
daemon never blocks the next start: the files it leaves behind are only files. The holder's
pid goes in a separate pid file, because on Windows a locked byte range cannot be read by
anyone else. The lock file itself is never deleted: removing it while another process has it
open would let two processes each lock a different file.

Stdlib and `findata.privfs` only.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType

from findata.privfs import ensure_private_dir

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        # flock (not fcntl/lockf) locks belong to the open file, so a second open in the same
        # process conflicts too, and they never pass to child processes.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class DaemonAlreadyRunning(RuntimeError):
    """Another live process holds the lock. `pid` is what its pid file says, if readable."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        who = f" (pid {pid})" if pid is not None else ""
        super().__init__(f"A findata daemon is already running for this data directory{who}.")


class DaemonLock:
    """Hold with `acquire()`/`release()` or as a context manager."""

    def __init__(self, path: Path, pid_path: Path | None = None) -> None:
        self.path = path
        self.pid_path = pid_path if pid_path is not None else path.with_suffix(".pid")
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock without waiting and record our pid; raise if a live process has it."""
        if self._fd is not None:
            raise RuntimeError("This DaemonLock is already held.")
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            locked = _try_lock(fd)
        except BaseException:
            os.close(fd)
            raise
        if not locked:
            os.close(fd)
            raise DaemonAlreadyRunning(self._read_pid())
        self._fd = fd
        try:
            self._write_pid()
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """Remove the pid file, then drop the lock. Safe to call when not held."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            # Still locked here, so this can never delete a newer daemon's pid file.
            self.pid_path.unlink(missing_ok=True)
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> DaemonLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _write_pid(self) -> None:
        fd = os.open(self.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()}\n")

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return None
