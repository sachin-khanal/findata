"""Opening the shared runtime for a command, with explicit errors for missing configuration."""

from __future__ import annotations

from findata import runtime
from findata.cli.output import fail
from findata.runtime import Runtime
from findata.schema import SchemaOutOfDateError


def connect(as_json: bool, *, need_plaid: bool = False) -> Runtime:
    """Build the runtime; with `need_plaid`, also fail early if Plaid keys are missing."""
    try:
        rt = runtime.build_runtime()
        if need_plaid:
            rt.gateway  # noqa: B018 (touching it validates the Plaid configuration)
    except SchemaOutOfDateError as exc:
        fail(as_json, str(exc), error_code="SCHEMA_OUT_OF_DATE")
    except RuntimeError as exc:  # e.g. PLAID_CLIENT_ID / PLAID_SECRET not set
        fail(as_json, str(exc), error_code="NOT_CONFIGURED")
    return rt
