"""`items list` / `items rm`: inspect and remove linked bank connections."""

from __future__ import annotations

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit, fail
from findata.client import DaemonClient

app = typer.Typer(no_args_is_help=True, help="List and remove linked bank connections.")


@app.command("list")
def items_list(as_json: bool = JsonOpt) -> None:
    """Show linked items with their account and transaction counts."""
    client = session.connect(as_json)
    with session.daemon_errors(as_json):
        items = client.items()
    if not items:
        emit(as_json, "No linked items.", event="items", items=[])
        return
    for item in items:
        emit(
            as_json,
            f"{item.id}  {item.institution_name or '-'}  "
            f"{item.accounts} accounts, {item.transactions} transactions",
            event="item",
            item_id=item.id,
            institution=item.institution_name,
            accounts=item.accounts,
            transactions=item.transactions,
        )


def _find_item(client: DaemonClient, prefix: str, as_json: bool):
    """The one linked item whose id starts with `prefix`; fails if none or several do."""
    with session.daemon_errors(as_json):
        matches = [i for i in client.items() if i.id.startswith(prefix)]
    if len(matches) != 1:
        fail(
            as_json,
            f"{len(matches)} items match {prefix!r}; run `findata items list` and use a "
            "unique id prefix."
            if matches
            else f"No item matches {prefix!r}.",
            error_code="NO_UNIQUE_MATCH",
        )
    return matches[0]


@app.command("rm")
def items_rm(
    item_id: str = typer.Argument(..., help="Item id, or a unique prefix of it."),
    local_only: bool = typer.Option(
        False, "--local-only", help="Skip revoking at Plaid; only delete the local copy."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    as_json: bool = JsonOpt,
) -> None:
    """Revoke an item at Plaid (/item/remove) and delete it and its data locally."""
    client = session.connect(as_json)
    item = _find_item(client, item_id, as_json)
    if not yes and not as_json:
        typer.confirm(
            f"Remove {item.institution_name} ({item.id}) and all its local data?", abort=True
        )
    session.announce_presence_prompt()
    with session.daemon_errors(
        as_json,
        what="the removal",
        plaid_suffix=". Nothing was deleted locally; fix the cause or retry with --local-only.",
    ):
        client.remove_item(item.id, local_only=local_only)
    emit(as_json, f"Removed {item_id}.", event="removed", item_id=item_id)
