"""Shared FastAPI dependencies for the daemon's routes.

The caller's role (from its credential header), permission checks against that role and the
current write grant, the user-presence check, the database session, the Plaid gateway, and the
error type every route raises. Routes declare `require(permission)`; nothing else decides who
may do what.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from findata.daemon.audit import SCOPE_KEY, AuditLog, RequestAudit
from findata.daemon.auth import ROLE_PERMISSIONS, CredentialStore, Permission, Role
from findata.daemon.events import KEEPALIVE_S, EventBus
from findata.daemon.grants import GRANTABLE, ApprovalStore, GrantStore
from findata.daemon.jobs import JobManager
from findata.daemon.presence import Presence, PresenceChecker
from findata.plaid_gateway import PlaidGateway
from findata.runtime import Runtime

logger = logging.getLogger(__name__)

CREDENTIAL_HEADER = "X-Findata-Credential"

# Held by everything that writes Plaid data (sync, link exchange, item removal), so two
# requests never write the same Item at once. Process-wide: one daemon, one writer.
PLAID_WRITE_LOCK = threading.Lock()


class DaemonError(Exception):
    """A refusal with a stable code, rendered as {"error_code", "detail", **extra}."""

    def __init__(self, status: int, error_code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.error_code = error_code
        self.detail = detail
        self.extra = extra


@dataclass
class DaemonState:
    runtime: Runtime
    credentials: CredentialStore
    grants: GrantStore
    approvals: ApprovalStore
    audit: AuditLog
    started_at: dt.datetime
    clock: Callable[[], dt.datetime]
    on_shutdown: Callable[[], None] | None
    presence: PresenceChecker
    events: EventBus
    jobs: JobManager
    job_sleep: Callable[[float], None] | None = None
    job_clock: Callable[[], float] | None = None
    keepalive_s: float = KEEPALIVE_S  # how long a silent stream waits before a keep-alive


def get_state(request: Request) -> DaemonState:
    return request.app.state.daemon


StateDep = Annotated[DaemonState, Depends(get_state)]


def get_session(state: StateDep) -> Iterator[Session]:
    with state.runtime.session_factory() as session:
        yield session


def get_gateway(state: StateDep) -> PlaidGateway:
    try:
        return state.runtime.gateway
    except RuntimeError as exc:  # Plaid keys not configured; everything else keeps working
        raise DaemonError(503, "NOT_CONFIGURED", str(exc)) from None


def resolve_role(request: Request) -> Role:
    """The caller's role from its credential header; 401 if missing or not a credential."""
    credentials = get_state(request).credentials
    try:
        role = credentials.role_for(request.headers.get(CREDENTIAL_HEADER))
    except UnicodeError:  # an undecodable header value is just a wrong credential
        role = None
    except (OSError, RuntimeError) as exc:  # a credential file is missing, loose or empty
        # The messages name the file and the fix, never a credential value.
        logger.error("Daemon credential files are unusable: %s", exc)
        raise DaemonError(
            500, "CREDENTIALS_UNAVAILABLE", f"The daemon cannot check credentials: {exc}"
        ) from None
    if role is None:
        raise DaemonError(401, "UNAUTHENTICATED", f"Missing or invalid {CREDENTIAL_HEADER} header.")
    facts: RequestAudit | None = request.scope.get(SCOPE_KEY)
    if facts is not None:
        facts.role = role
    return role


RoleDep = Annotated[Role, Depends(resolve_role)]
SessionDep = Annotated[Session, Depends(get_session)]
GatewayDep = Annotated[PlaidGateway, Depends(get_gateway)]


def _write_not_granted(permission: str) -> DaemonError:
    return DaemonError(
        403,
        "WRITE_NOT_GRANTED",
        f"The agent (mcp) role may not use {permission!r} until a human allows it. Ask the "
        f"user to run `findata grant mcp-write --ops {permission}` in a terminal; an agent "
        "cannot grant itself access. Nothing was changed.",
    )


_PRESENCE_REFUSALS: dict[Presence, str] = {
    "denied": "Not confirmed at the Mac (Touch ID or password was cancelled or timed out). "
    "Nothing was changed.",
    "not_possible": "This needs confirmation at the Mac, and this session cannot show the "
    "Touch ID or password prompt (for example over SSH). Run it from a logged-in session on "
    "the Mac.",
}


def presence_gate(request: Request, role: RoleDep, state: StateDep) -> Callable[[str], None]:
    """The user-presence check for an operation that carries human authority.

    The route calls the returned function once its own checks have passed, before it changes
    anything or takes `PLAID_WRITE_LOCK`. For `cli` and `web` it asks the user to confirm
    `reason` at the Mac and raises USER_PRESENCE_REQUIRED unless they do (or the platform has
    no check); the outcome goes into the request's audit entry. The agent (mcp) is never
    asked: its only gated route is a removal whose approval was already confirmed in person.
    """

    def confirm(reason: str) -> None:
        if role == "mcp":
            return
        outcome = state.presence.confirm(reason)
        facts: RequestAudit | None = request.scope.get(SCOPE_KEY)
        if facts is not None:
            facts.presence = outcome
        if outcome not in ("confirmed", "unsupported"):  # anything else is a refusal
            detail = _PRESENCE_REFUSALS.get(outcome, _PRESENCE_REFUSALS["denied"])
            raise DaemonError(403, "USER_PRESENCE_REQUIRED", detail)

    return confirm


PresenceDep = Annotated[Callable[[str], None], Depends(presence_gate)]


def require(permission: Permission) -> Any:
    """A route dependency admitting only callers whose role may use `permission`.

    `web` and `cli` may do everything. `mcp` may always read; it may sync, link or remove
    items only while a human's grant covers that operation, and never use admin routes.
    """

    def check(request: Request, role: RoleDep) -> Role:
        if permission in ROLE_PERMISSIONS[role]:
            return role
        if role == "mcp" and permission in GRANTABLE:
            if get_state(request).grants.allows(permission):
                return role
            raise _write_not_granted(permission)
        raise DaemonError(403, "FORBIDDEN", f"The {role} role may not use this operation.")

    return Depends(check)
