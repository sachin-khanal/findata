"""`findata` command-line interface."""

from __future__ import annotations

import json
from typing import NoReturn

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


def _emit(as_json: bool, human: str, **event) -> None:
    """One line of output: a JSON object per line for scripts/agents, else plain text."""
    typer.echo(json.dumps(event, default=str) if as_json else human)


def _fail(as_json: bool, message: str, code: int = 1, **event) -> NoReturn:
    """Report an error explicitly and exit non-zero (stderr for people, JSON line for scripts)."""
    if as_json:
        typer.echo(json.dumps({"event": "error", "message": message, **event}))
    else:
        typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code)


def _connect(as_json: bool):
    """Open dependencies, turning a missing-config failure into an explicit message."""
    try:
        return _deps()
    except RuntimeError as exc:  # e.g. PLAID_CLIENT_ID / PLAID_SECRET not set
        _fail(as_json, str(exc), error_code="NOT_CONFIGURED")


JsonOpt = typer.Option(False, "--json", help="Machine-readable output: one JSON object per line.")


@app.command("sandbox-link")
def sandbox_link(
    username: str = typer.Option("user_transactions_dynamic", help="Plaid sandbox test user"),
    timeout: float = typer.Option(120.0, min=0, help="Max seconds to wait for history to load."),
    interval: float = typer.Option(5.0, min=0.5, help="Seconds between checks while waiting."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for Plaid to finish loading."),
    as_json: bool = JsonOpt,
) -> None:
    """Link a Sandbox test bank (no browser) and sync it, waiting until history has loaded.

    Exit codes: 0 history fully loaded, 2 linked but still loading when the wait ended
    (run `findata sync` later), 1 error.
    """
    from findata.plaid_gateway import PlaidError
    from findata.sync import link_item, sync_item, wait_for_history

    _, session_factory, gateway, cipher = _connect(as_json)
    try:
        with session_factory() as session:
            _emit(
                as_json,
                f"Linking Plaid Sandbox test user {username!r}…",
                event="linking",
                username=username,
            )
            item = link_item(session, gateway, cipher, gateway.sandbox_public_token(username))
            _emit(
                as_json,
                f"Linked {item.institution_name} (item {item.id}).",
                event="linked",
                item_id=item.id,
                institution=item.institution_name,
            )
            if not wait:
                r = sync_item(session, gateway, cipher, item)
                _emit(
                    as_json,
                    f"Synced once: +{r.added} transactions. {r.detail}",
                    event="done",
                    added=r.added,
                    status=r.status,
                    history_complete=r.history_complete,
                    timed_out=False,
                )
                raise typer.Exit(0 if r.history_complete else 2)

            def progress(n, r, elapsed):
                _emit(
                    as_json,
                    f"  [{elapsed:5.1f}s] check {n}: +{r.added} new. {r.detail}",
                    event="poll",
                    attempt=n,
                    elapsed_s=round(elapsed, 1),
                    added=r.added,
                    status=r.status,
                    history_complete=r.history_complete,
                )

            _emit(
                as_json,
                "Waiting for Plaid to load transaction history "
                f"(checking every {interval:g}s, up to {timeout:g}s)…",
                event="waiting",
                interval_s=interval,
                timeout_s=timeout,
            )
            out = wait_for_history(
                session, gateway, cipher, item, timeout=timeout, interval=interval, on_poll=progress
            )
    except PlaidError as exc:
        _fail(
            as_json,
            f"Plaid rejected the request ({exc.error_code}): {exc}",
            error_code=exc.error_code,
        )
    if out.history_complete:
        _emit(
            as_json,
            f"Done: {out.added} transactions loaded in {out.elapsed:.0f}s. "
            "Next: `findata txns` to list them, or `findata serve` for the web UI.",
            event="done",
            added=out.added,
            status=out.status,
            history_complete=True,
            timed_out=False,
            polls=out.polls,
        )
        return
    _emit(
        as_json,
        f"Linked, but Plaid was still loading history after {out.elapsed:.0f}s "
        f"({out.added} transactions so far). Nothing is lost: run `findata sync` again in a "
        "minute or two to fetch the rest.",
        event="done",
        added=out.added,
        status=out.status,
        history_complete=False,
        timed_out=True,
        polls=out.polls,
    )
    raise typer.Exit(2)


@app.command()
def sync(as_json: bool = JsonOpt) -> None:
    """Pull new/changed/removed transactions for every linked item."""
    from findata.plaid_gateway import PlaidError
    from findata.sync import sync_all

    _, session_factory, gateway, cipher = _connect(as_json)
    try:
        with session_factory() as session:
            results = sync_all(session, gateway, cipher)
    except PlaidError as exc:
        _fail(
            as_json,
            f"Plaid rejected the request ({exc.error_code}): {exc}",
            error_code=exc.error_code,
        )
    if not results:
        _emit(
            as_json,
            "No linked items yet. Run `findata sandbox-link` or link one in the "
            "web UI (`findata serve`).",
            event="synced",
            items=[],
        )
        return
    for r in results:
        _emit(
            as_json,
            f"{r.item_id}: +{r.added} ~{r.modified} -{r.removed}. {r.detail}",
            event="item",
            item_id=r.item_id,
            added=r.added,
            modified=r.modified,
            removed=r.removed,
            status=r.status,
            history_complete=r.history_complete,
        )
    if not all(r.history_complete for r in results):
        _emit(
            as_json,
            "Some items are still loading history; run `findata sync` again shortly.",
            event="note",
            history_complete=False,
        )


@app.command()
def txns(
    limit: int = typer.Option(20, min=1, max=500),
    as_json: bool = JsonOpt,
) -> None:
    """Print recent transactions."""
    from sqlalchemy import select

    from findata.db import Transaction, make_engine, make_session_factory

    s = get_settings()
    with make_session_factory(make_engine(s.db_path))() as session:
        rows = list(
            session.scalars(
                select(Transaction).order_by(Transaction.date.desc(), Transaction.id).limit(limit)
            )
        )
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


if __name__ == "__main__":
    app()
