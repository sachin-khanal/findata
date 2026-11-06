"""`audit`: what the daemon recorded about who did what."""

from __future__ import annotations

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit


def audit(
    limit: int = typer.Option(20, min=1, max=1000, help="How many of the newest entries."),
    as_json: bool = JsonOpt,
) -> None:
    """Show the newest audit entries (who asked for what, and whether it went through)."""
    client = session.connect(as_json)
    with session.daemon_errors(as_json):
        entries = client.audit(limit)
    if not entries:
        emit(as_json, "No audit entries yet.", event="audit", entries=[])
        return
    for e in entries:
        emit(
            as_json,
            f"{e['at']}  {e['role']:<4}  {e['operation']:<20}  {e['outcome']:<8}  {e['summary']}",
            event="audit",
            **e,
        )
