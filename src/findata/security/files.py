"""Owner-only files and directories, and loading/creating secret files."""

from __future__ import annotations

import os
from pathlib import Path

# POSIX permission bits are meaningless on Windows (st_mode always looks world-accessible);
# there the protection is the per-user profile ACL that the data directory inherits.
_POSIX = os.name == "posix"


def ensure_private_dir(path: Path) -> None:
    """Create `path` owner-only, or refuse an existing one that others can enter.

    mkdir's mode is masked by the umask, applies only to the last component, and is ignored
    when the directory already exists, so the result is checked rather than assumed. An
    owner-only directory protects everything inside it regardless of its parents' modes.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat().st_mode & 0o777
    if _POSIX and mode & 0o077:
        raise PermissionError(
            f"{path} is accessible by other users (mode {oct(mode)}). Run: chmod 700 {path}"
        )


def create_private_file(path: Path) -> None:
    """Make sure `path` exists, owner-only from its first byte; refuse a looser existing one."""
    ensure_private_dir(path.parent)
    try:
        # O_EXCL + mode: the file never exists with broader permissions, even briefly.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        mode = path.stat().st_mode & 0o777
        if _POSIX and mode & 0o077:
            raise PermissionError(
                f"{path} is readable by other users (mode {oct(mode)}). Run: chmod 600 {path}"
            ) from None


def write_secret_file(path: Path, data: bytes) -> None:
    ensure_private_dir(path.parent)
    # O_EXCL: never silently overwrite an existing key (that would orphan encrypted data).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def read_secret_file(path: Path) -> bytes:
    mode = path.stat().st_mode & 0o777
    if _POSIX and mode & 0o077:
        raise PermissionError(
            f"{path} is readable by other users (mode {oct(mode)}). Run: chmod 600 {path}"
        )
    return path.read_bytes().strip()


def load_or_create_secret(path: Path, factory) -> bytes:
    if path.exists():
        return read_secret_file(path)
    value = factory()
    write_secret_file(path, value)
    return value
