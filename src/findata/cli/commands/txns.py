"""`txns`: print recent transactions from the local database."""

from __future__ import annotations

import json

import typer

from findata import queries
from findata.cli.output import JsonOpt
from findata.cli.session import connect


def txns(
    limit: int = typer.Option(20, min=1, max=500),
    as_json: bool = JsonOpt,
) -> None:
    """Print recent transactions."""
    rt = connect(as_json)
    with rt.session_factory() as session:
        rows = queries.list_transactions(session, limit=limit)
    if as_json:
        for t in rows:
            typer.echo(
                json.dumps(
                    {
                        "id": t.id,
                        "date": t.date,
                        "amount": t.amount,
                        "name": t.name,
                        "merchant_name": t.merchant_name,
                        "category": t.category_primary,
                    },
                    default=str,
                )
            )
        return
    if not rows:
        typer.echo(
            "No transactions stored yet. If you just linked an account, Plaid may "
            "still be loading it: run `findata sync`. If nothing is linked, run "
            "`findata sandbox-link`.",
            err=True,
        )
        return
    for t in rows:
        typer.echo(
            f"{t.date}  {t.amount:>10}  {(t.category_primary or ''):<24}  "
            f"{t.merchant_name or t.name}"
        )
