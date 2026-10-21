"""`findata` command-line interface: assembles one command module per concern."""

from __future__ import annotations

import typer

from findata.cli.commands import items, migrate, sandbox_link, setup, sync, txns

app = typer.Typer(no_args_is_help=True, help="Local-first personal finance data on Plaid.")

app.command()(setup.init)
app.command()(setup.serve)
app.command("sandbox-link")(sandbox_link.sandbox_link)
app.add_typer(items.app, name="items")
app.command()(migrate.migrate)
app.command()(sync.sync)
app.command()(txns.txns)

__all__ = ["app"]
