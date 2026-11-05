"""`start`: run the daemon, the one process that holds the key, the database and Plaid access.

Core modules are imported inside the command, never at module level, so importing
`findata.cli` does not load key or database code (front ends must never hold them).
"""

from __future__ import annotations

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit, fail


def _start_in_background(wait_timeout: float) -> None:
    import os

    from findata.cli import background
    from findata.locations import ClientSettings

    if os.name == "nt":
        fail(
            False,
            "--background is not supported on Windows yet; "
            "run `findata start` in its own terminal.",
        )
    log = ClientSettings().locations.log
    try:
        started = background.start_in_background(log, wait_timeout)
    except (background.BackgroundStartError, OSError) as exc:
        fail(False, str(exc))
    typer.echo(f"findata daemon started in the background (pid {started.pid}). Log: {started.log}")


def start(
    background: bool = typer.Option(
        False, "--background", help="Detach: return once the daemon is ready (log: daemon.log)."
    ),
    wait_timeout: float = typer.Option(
        30.0, "--wait-timeout", min=1, help="With --background: seconds to wait for readiness."
    ),
    ready_fd: int | None = typer.Option(None, "--ready-fd", hidden=True),
) -> None:
    """Run the findata daemon in the foreground (Ctrl+C stops it)."""
    if background:
        if ready_fd is not None:
            fail(False, "--background and --ready-fd cannot be combined.")
        _start_in_background(wait_timeout)
        return

    from findata.config import get_settings
    from findata.daemon.server import configure_logging, run_daemon

    configure_logging()
    try:
        run_daemon(get_settings(), ready_fd=ready_fd)
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
