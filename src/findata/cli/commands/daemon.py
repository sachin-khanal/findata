"""`start`: run the daemon, the one process that holds the key, the database and Plaid access.

Core modules are imported inside the command, never at module level, so importing
`findata.cli` does not load key or database code (front ends must never hold them).
"""

from __future__ import annotations

from findata.cli.output import fail


def start() -> None:
    """Run the findata daemon in the foreground (Ctrl+C stops it)."""
    from findata.config import get_settings
    from findata.daemon.server import configure_logging, run_daemon

    configure_logging()
    try:
        run_daemon(get_settings())
    except (RuntimeError, OSError) as exc:
        # Already running (names the pid), schema behind (says `findata migrate`), no master
        # key, socket path too long, a data directory others can enter: each message says
        # what to do, so no traceback.
        fail(False, str(exc))
