"""`findata` command-line interface."""

from __future__ import annotations

import time

import typer

from findata.config import get_settings

app = typer.Typer(no_args_is_help=True, help="Local-first personal finance data on Plaid.")


def _deps():
    from findata.db import make_engine, make_session_factory
    from findata.plaid_gateway import PlaidClient
    from findata.security import TokenCipher

    settings = get_settings()
    engine = make_engine(settings.db_path)
    return (
        settings,
        make_session_factory(engine),
        PlaidClient(settings),
        TokenCipher.from_key_file(settings.key_path),
    )


@app.command()
def init() -> None:
    """Create the data directory, encryption key, local API token and database."""
    from findata.db import make_engine
    from findata.security import TokenCipher, load_or_create_api_token

    s = get_settings()
    TokenCipher.from_key_file(s.key_path)
    load_or_create_api_token(s.api_token_path)
    make_engine(s.db_path)
    typer.echo(f"Initialized {s.data_dir}")


@app.command()
def serve() -> None:
    """Run the local web UI + API on 127.0.0.1."""
    import uvicorn

    from findata.api import create_app

    s = get_settings()
    typer.echo(f"findata ({s.plaid_env}) → http://{s.findata_host}:{s.findata_port}")
    uvicorn.run(create_app(s), host=s.findata_host, port=s.findata_port)


@app.command("sandbox-link")
def sandbox_link(
    username: str = typer.Option("user_transactions_dynamic", help="Plaid sandbox test user"),
) -> None:
    """Create a Sandbox item without the browser (First Platypus Bank) and sync it."""
    from findata.sync import link_item, sync_item

    _, session_factory, gateway, cipher = _deps()
    with session_factory() as session:
        item = link_item(session, gateway, cipher, gateway.sandbox_public_token(username))
        typer.echo(f"Linked {item.institution_name} ({item.id}). Waiting for transactions…")
        for _ in range(10):  # Sandbox data can take a few seconds to become ready
            result = sync_item(session, gateway, cipher, item)
            if result.status != "NOT_READY" or result.added:
                break
            time.sleep(3)
        typer.echo(f"+{result.added} ~{result.modified} -{result.removed} ({result.status})")


@app.command()
def sync() -> None:
    """Pull new/changed/removed transactions for every linked item."""
    from findata.sync import sync_all

    _, session_factory, gateway, cipher = _deps()
    with session_factory() as session:
        for r in sync_all(session, gateway, cipher):
            typer.echo(f"{r.item_id}: +{r.added} ~{r.modified} -{r.removed} ({r.status})")


@app.command()
def txns(limit: int = typer.Option(20, min=1, max=500)) -> None:
    """Print recent transactions."""
    from sqlalchemy import select

    from findata.db import Transaction, make_engine, make_session_factory

    s = get_settings()
    with make_session_factory(make_engine(s.db_path))() as session:
        q = select(Transaction).order_by(Transaction.date.desc()).limit(limit)
        for t in session.scalars(q):
            typer.echo(
                f"{t.date}  {t.amount:>10}  {(t.category_primary or ''):<24}  "
                f"{t.merchant_name or t.name}"
            )


if __name__ == "__main__":
    app()
