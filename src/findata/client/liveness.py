"""Is a daemon alive for this data directory? Probes the daemon's single-instance lock.

Mirrors the lock in `findata.daemon.lock` (`flock` on POSIX, `msvcrt.locking` on Windows),
re-implemented here so the client never imports daemon internals. Stdlib only.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

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
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def daemon_lock_held(lock_path: Path) -> bool:
    """True only if another process holds the lock. A missing lock file means no daemon."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)  # we got it, so nobody else had it; release immediately
            return False
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
