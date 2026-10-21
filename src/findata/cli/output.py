"""How the CLI talks: plain text for people, one JSON object per line for scripts/agents."""

from __future__ import annotations

import json
from typing import NoReturn

import typer

JsonOpt = typer.Option(False, "--json", help="Machine-readable output: one JSON object per line.")


def emit(as_json: bool, human: str, **event) -> None:
    """One line of output: a JSON object per line for scripts/agents, else plain text."""
    typer.echo(json.dumps(event, default=str) if as_json else human)


def fail(as_json: bool, message: str, code: int = 1, **event) -> NoReturn:
    """Report an error explicitly and exit non-zero (stderr for people, JSON line for scripts)."""
    if as_json:
        typer.echo(json.dumps({"event": "error", "message": message, **event}))
    else:
        typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code)


def plaid_rejected(as_json: bool, exc, what: str = "the request", suffix: str = "") -> NoReturn:
    """Standard failure for a PlaidError."""
    fail(
        as_json,
        f"Plaid rejected {what} ({exc.error_code}): {exc}{suffix}",
        error_code=exc.error_code,
    )
