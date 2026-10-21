"""Daemon status and lifecycle: `GET /v1/status`, `POST /v1/shutdown`."""

from __future__ import annotations

from importlib.metadata import version as package_version

from fastapi import APIRouter

import findata
from findata.daemon.deps import StateDep, require
from findata.schema import current_revision
from findata.wire import StatusOut

router = APIRouter(prefix="/v1")


def _version() -> str:
    return getattr(findata, "__version__", None) or package_version("findata")


@router.get("/status", name="status", dependencies=[require("read")])
def status(state: StateDep) -> StatusOut:
    runtime = state.runtime
    return StatusOut(
        version=_version(),
        plaid_env=runtime.settings.plaid_env,
        schema_revision=current_revision(runtime.session_factory.kw["bind"]),
        started_at=state.started_at,
        uptime_s=(state.clock() - state.started_at).total_seconds(),
    )


@router.post("/shutdown", name="shutdown", status_code=202, dependencies=[require("admin")])
def shutdown(state: StateDep) -> dict[str, str]:
    if state.on_shutdown is not None:
        state.on_shutdown()
    return {"status": "stopping"}
