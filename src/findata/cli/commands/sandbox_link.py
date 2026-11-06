"""`sandbox-link`: link a Plaid Sandbox bank without a browser, then wait for history.

The daemon does the work as a job; this command prints the job's events as they arrive.
"""

from __future__ import annotations

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit, fail
from findata.client import DaemonRejected
from findata.wire import SandboxLinkIn, describe_status


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
    client = session.connect(as_json)
    body = SandboxLinkIn(username=username, new=new, wait=wait, timeout=timeout, interval=interval)
    with session.daemon_errors(as_json):
        try:
            job_id = client.start_sandbox_link(body)
        except DaemonRejected as exc:
            if exc.error_code != "JOB_ALREADY_RUNNING":
                raise
            fail(
                as_json,
                "A sandbox-link or history job is already running (one at a time). Wait for "
                "it to finish, then run this again.",
                error_code=exc.error_code,
                **exc.extra,
            )
        history_complete = False
        last_elapsed = 0.0
        for event in client.follow_job(job_id):
            name = event.get("event")
            if name == "poll":
                last_elapsed = event["elapsed_s"]
            if name == "error":
                fail(as_json, event["message"], error_code=event["error_code"])
            if name == "done":
                history_complete = event["history_complete"]
                _show_done(as_json, event, wait, last_elapsed)
            elif name != "job_finished":
                _show(as_json, event)
    if not history_complete:
        raise typer.Exit(2)


def _show(as_json: bool, event: dict) -> None:
    """Print one job event: the same line of text or JSON object the CLI has always shown."""
    name = event["event"]
    if name == "reused":
        human = (
            f"{event['institution']} is already linked (item {event['item_id']}); reusing it. "
            "Use --new to link another, or `findata items rm` to remove it."
        )
    elif name == "linking":
        human = f"Linking Plaid Sandbox test user {event['username']!r}…"
    elif name == "linked":
        human = f"Linked {event['institution']} (item {event['item_id']})."
    elif name == "waiting":
        human = (
            "Waiting for Plaid to load transaction history "
            f"(checking every {event['interval_s']:g}s, up to {event['timeout_s']:g}s)…"
        )
    elif name == "poll":
        human = (
            f"  [{event['elapsed_s']:5.1f}s] check {event['attempt']}: +{event['added']} new. "
            f"{describe_status(event['status'])}"
        )
    else:
        human = name
    emit(as_json, human, **event)


def _show_done(as_json: bool, event: dict, waited: bool, elapsed: float) -> None:
    added = event["added"]
    if not waited:
        human = f"Synced once: +{added} transactions. {describe_status(event['status'])}"
    elif event["history_complete"]:
        human = (
            f"Done: {added} transactions loaded in {elapsed:.0f}s. "
            "Next: `findata txns` to list them, or `findata serve` for the web UI."
        )
    else:
        human = (
            f"Linked, but Plaid was still loading history after {elapsed:.0f}s "
            f"({added} transactions so far). Nothing is lost: run `findata sync` again in a "
            "minute or two to fetch the rest."
        )
    emit(as_json, human, **event)
