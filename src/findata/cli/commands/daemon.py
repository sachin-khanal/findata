"""`start`: run the daemon, the one process that holds the key, the database and Plaid access.

Core modules are imported inside the command, never at module level, so importing
`findata.cli` does not load key or database code (front ends must never hold them).
"""

from __future__ import annotations

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit, fail


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


def stop() -> None:
    """Ask the running daemon to shut down cleanly."""
    client = session.connect(False)
    with session.daemon_errors(False):
        client.shutdown()
    typer.echo("Stopping the findata daemon.")


def status(as_json: bool = JsonOpt) -> None:
    """Show the running daemon's version, Plaid environment, schema, uptime and user presence."""
    client = session.connect(as_json)
    with session.daemon_errors(as_json):
        s = client.status()
    emit(
        as_json,
        f"findata {s.version} ({s.plaid_env}), schema {s.schema_revision}, "
        f"up {s.uptime_s:.0f}s, user presence: {s.user_presence}",
        event="status",
        **s.model_dump(mode="json"),
    )
