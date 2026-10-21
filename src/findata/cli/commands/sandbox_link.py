"""`sandbox-link`: link a Plaid Sandbox bank without a browser, then wait for history."""

from __future__ import annotations

import typer

from findata.cli.output import JsonOpt, emit, plaid_rejected
from findata.cli.session import connect
from findata.linking import find_sandbox_item, link_sandbox_item
from findata.plaid_gateway import PlaidError
from findata.sync import sync_item
from findata.waiting import wait_for_history


def sandbox_link(
    username: str = typer.Option("user_transactions_dynamic", help="Plaid sandbox test user"),
    timeout: float = typer.Option(120.0, min=0, help="Max seconds to wait for history to load."),
    interval: float = typer.Option(5.0, min=0.5, help="Seconds between checks while waiting."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for Plaid to finish loading."),
    new: bool = typer.Option(
        False, "--new", help="Link another Item even if the Sandbox bank is already linked."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Link a Sandbox test bank (no browser) and sync it, waiting until history has loaded.

    If the Sandbox bank is already linked it is reused (each link would otherwise create a
    duplicate Item at Plaid); pass --new to link another anyway.

    Exit codes: 0 history fully loaded, 2 linked but still loading when the wait ended
    (run `findata sync` later), 1 error.
    """
    rt = connect(as_json, need_plaid=True)
    try:
        with rt.session_factory() as session:
            item = None if new else find_sandbox_item(session)
            if item is not None:
                emit(
                    as_json,
                    f"{item.institution_name} is already linked (item {item.id}); reusing it. "
                    "Use --new to link another, or `findata items rm` to remove it.",
                    event="reused",
                    item_id=item.id,
                    institution=item.institution_name,
                )
            else:
                emit(
                    as_json,
                    f"Linking Plaid Sandbox test user {username!r}…",
                    event="linking",
                    username=username,
                )
                item = link_sandbox_item(session, rt.gateway, rt.cipher, username)
                emit(
                    as_json,
                    f"Linked {item.institution_name} (item {item.id}).",
                    event="linked",
                    item_id=item.id,
                    institution=item.institution_name,
                )
            if not wait:
                r = sync_item(session, rt.gateway, rt.cipher, item)
                emit(
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
                emit(
                    as_json,
                    f"  [{elapsed:5.1f}s] check {n}: +{r.added} new. {r.detail}",
                    event="poll",
                    attempt=n,
                    elapsed_s=round(elapsed, 1),
                    added=r.added,
                    status=r.status,
                    history_complete=r.history_complete,
                )

            emit(
                as_json,
                "Waiting for Plaid to load transaction history "
                f"(checking every {interval:g}s, up to {timeout:g}s)…",
                event="waiting",
                interval_s=interval,
                timeout_s=timeout,
            )
            out = wait_for_history(
                session,
                rt.gateway,
                rt.cipher,
                item,
                timeout=timeout,
                interval=interval,
                on_poll=progress,
            )
    except PlaidError as exc:
        plaid_rejected(as_json, exc)
    if out.history_complete:
        emit(
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
    emit(
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
