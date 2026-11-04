"""`grant`, `revoke`, `approve`: the human's controls over what the agent (MCP) may write.

Granting and approving widen the agent's powers, so the daemon asks for Touch ID (or the
password) at the Mac first; revoking only narrows them and asks nothing.
"""

from __future__ import annotations

import re

import typer

from findata.cli import session
from findata.cli.output import JsonOpt, emit, fail
from findata.client import DaemonRejected
from findata.wire import MAX_GRANT_TTL_S

TARGET = "mcp-write"
_UNITS = {"s": 1, "m": 60, "h": 3600}


def _check_target(as_json: bool, target: str) -> None:
    if target != TARGET:
        fail(
            as_json,
            f"Unknown target {target!r}; the only one is `{TARGET}` (the agent's write access).",
            error_code="INVALID_ARGUMENT",
        )


def _parse_duration(as_json: bool, text: str) -> int:
    """Seconds in `Ns`, `Nm` or `Nh`; a grant is positive and lasts at most 24 hours."""
    m = re.fullmatch(r"(\d+)([smh])", text.strip())
    seconds = int(m[1]) * _UNITS[m[2]] if m else 0
    if not m or seconds <= 0:
        fail(
            as_json,
            f"{text!r} is not a duration; use a number with s, m or h, such as 90s, 15m or 2h.",
            error_code="INVALID_DURATION",
        )
    if seconds > MAX_GRANT_TTL_S:
        fail(
            as_json,
            f"A grant lasts at most 24h; {text!r} is longer. Grant again when it runs out.",
            error_code="INVALID_DURATION",
        )
    return seconds


def grant(
    target: str = typer.Argument(..., help=f"What to grant: `{TARGET}`."),
    ops: str = typer.Option(
        "sync",
        "--ops",
        help="Comma-separated operations: sync, link, items.remove (the default is sync only).",
    ),
    duration: str = typer.Option("15m", "--for", help="How long it lasts: 90s, 15m, 2h (max 24h)."),
    as_json: bool = JsonOpt,
) -> None:
    """Let the agent write for a while (asks for Touch ID at the Mac first)."""
    _check_target(as_json, target)
    ttl_s = _parse_duration(as_json, duration)
    operations = [op.strip() for op in ops.split(",") if op.strip()]
    client = session.connect(as_json)
    session.announce_presence_prompt()
    with session.daemon_errors(as_json):
        out = client.grant(operations, ttl_s)
    expires = out.expires_at.astimezone().strftime("%H:%M:%S")
    emit(
        as_json,
        f"The agent may {', '.join(out.operations)} until {expires} ({duration}). "
        f"Revoke early with `findata revoke {TARGET}`.",
        event="granted",
        operations=out.operations,
        expires_at=out.expires_at,
    )


def revoke(
    target: str = typer.Argument(..., help=f"What to revoke: `{TARGET}`."),
    as_json: bool = JsonOpt,
) -> None:
    """End the agent's write access now."""
    _check_target(as_json, target)
    client = session.connect(as_json)
    with session.daemon_errors(as_json):
        client.revoke()
    emit(as_json, "The agent's write access is revoked.", event="revoked")


def approve(
    request_id: str = typer.Argument(..., help="The request id the agent was given."),
    as_json: bool = JsonOpt,
) -> None:
    """Approve one removal the agent asked for (asks for Touch ID at the Mac first)."""
    client = session.connect(as_json)
    session.announce_presence_prompt()
    with session.daemon_errors(as_json):
        try:
            client.approve(request_id)
        except DaemonRejected as exc:
            if exc.error_code != "NOT_FOUND":
                raise
            fail(
                as_json,
                f"No pending request {request_id!r}: it may have expired, been approved "
                "already (the agent should retry its removal), or been used.",
                error_code=exc.error_code,
            )
    emit(
        as_json,
        f"Approved {request_id}. The agent can now retry the removal.",
        event="approved",
        request_id=request_id,
    )
