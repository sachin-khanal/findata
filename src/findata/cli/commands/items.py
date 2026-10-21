"""`items list` / `items rm`: inspect and remove linked bank connections."""

from __future__ import annotations

import typer

from findata import queries
from findata.cli.output import JsonOpt, emit, fail, plaid_rejected
from findata.cli.session import connect
from findata.linking import remove_item
from findata.plaid_gateway import PlaidError

app = typer.Typer(no_args_is_help=True, help="List and remove linked bank connections.")


@app.command("list")
def items_list(as_json: bool = JsonOpt) -> None:
    """Show linked items with their account and transaction counts."""
    rt = connect(as_json)
    with rt.session_factory() as session:
        summaries = queries.list_item_summaries(session)
    if not summaries:
        emit(as_json, "No linked items.", event="items", items=[])
        return
    for s in summaries:
        item = s.item
        emit(
            as_json,
            f"{item.id}  {item.institution_name or '-'}  "
            f"{s.accounts} accounts, {s.transactions} transactions",
            event="item",
            item_id=item.id,
            institution=item.institution_name,
            accounts=s.accounts,
            transactions=s.transactions,
        )


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
    rt = connect(as_json, need_plaid=not local_only)
    with rt.session_factory() as session:
        matches = queries.find_items_by_prefix(session, item_id)
        if len(matches) != 1:
            fail(
                as_json,
                f"{len(matches)} items match {item_id!r}; run `findata items list` and use a "
                "unique id prefix."
                if matches
                else f"No item matches {item_id!r}.",
                error_code="NO_UNIQUE_MATCH",
            )
        item = matches[0]
        if not yes and not as_json:
            typer.confirm(
                f"Remove {item.institution_name} ({item.id}) and all its local data?", abort=True
            )
        try:
            remove_item(
                session,
                None if local_only else rt.gateway,
                rt.cipher,
                item,
                local_only=local_only,
            )
        except PlaidError as exc:
            plaid_rejected(
                as_json,
                exc,
                "the removal",
                ". Nothing was deleted locally; fix the cause or retry with --local-only.",
            )
    emit(as_json, f"Removed {item_id}.", event="removed", item_id=item_id)
