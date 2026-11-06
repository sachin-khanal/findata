"""Re-export of the owner-only file helpers, which now live in `findata.privfs`."""

from findata.privfs import (
    create_private_file,
    ensure_private_dir,
    load_or_create_secret,
    read_secret_file,
    write_secret_file,
)

__all__ = [
    "create_private_file",
    "ensure_private_dir",
    "load_or_create_secret",
    "read_secret_file",
    "write_secret_file",
]
