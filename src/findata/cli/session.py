"""Reaching the daemon for a command, and turning the ways that can fail into CLI errors.

Imports only the client, locations and CLI helpers: a front end never loads the key, the
database or Plaid.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

import typer

from findata.cli.output import fail, plaid_rejected
from findata.client import (
    DaemonClient,
    DaemonClientError,
    DaemonNotRunning,
    DaemonRejected,
    DaemonTimeout,
)
from findata.locations import ClientSettings

PRESENCE_NOTICE = "Confirm on your Mac (Touch ID or password)…"
PLAID_REJECTED = 502  # the daemon's status for an error Plaid returned


def connect(as_json: bool) -> DaemonClient:
    """A client for the daemon as the `cli` role. It makes no call, so nothing fails yet:
    wrap the calls in `daemon_errors`."""
    return DaemonClient(ClientSettings().data_dir, "cli")


def announce_presence_prompt() -> None:
    """Tell the person where to look while the daemon waits for Touch ID (stderr, so never
    mixed into `--json` output). Only macOS has a presence check, so elsewhere it says nothing."""
    if sys.platform == "darwin":
        typer.echo(PRESENCE_NOTICE, err=True)


@contextmanager
def daemon_errors(
    as_json: bool, *, what: str = "the request", plaid_suffix: str = ""
) -> Iterator[None]:
    """Report a failed daemon call explicitly (message and `error_code`, no traceback) and exit 1.

    An error from Plaid reads "Plaid rejected {what} (CODE): ..." with `plaid_suffix` after it.
    """
    try:
        yield
    except DaemonNotRunning as exc:
        fail(as_json, str(exc), error_code="DAEMON_NOT_RUNNING")
    except DaemonTimeout as exc:
        fail(as_json, str(exc), error_code="DAEMON_TIMEOUT")
    except DaemonRejected as exc:
        if exc.status == PLAID_REJECTED:
            plaid_rejected(as_json, exc.error_code, exc.detail, what, plaid_suffix)
        fail(as_json, exc.detail, error_code=exc.error_code, **exc.extra)
    except DaemonClientError as exc:  # e.g. no credential file yet
        fail(as_json, str(exc), error_code="NOT_CONFIGURED")
