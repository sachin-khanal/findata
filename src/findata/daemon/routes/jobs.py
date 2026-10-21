"""Long-running jobs: link a Plaid Sandbox bank and wait for its history, polled by job id."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from findata.daemon.deps import PLAID_WRITE_LOCK, DaemonError, GatewayDep, StateDep, require
from findata.daemon.jobs import JobAlreadyRunning
from findata.linking import find_sandbox_item, link_sandbox_item
from findata.plaid_gateway import PlaidGateway
from findata.runtime import Runtime
from findata.sync import sync_item
from findata.waiting import wait_for_history
from findata.wire import JobOut, SandboxLinkIn

DEFAULT_SANDBOX_USER = "user_transactions_dynamic"

router = APIRouter(prefix="/v1/jobs")

SANDBOX_LINK_JOB = "sandbox-link"  # at most one of these runs at a time

Emit = Callable[[dict], None]


def sandbox_link_job(
    runtime: Runtime,
    gateway: PlaidGateway,
    body: SandboxLinkIn,
    emit: Emit,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], float] | None,
) -> dict:
    """The old `sandbox-link` CLI flow, with the same events; returns the job's result."""
    username = body.username or DEFAULT_SANDBOX_USER
    cipher = runtime.cipher
    # The write lock is held for each Plaid/DB write step, never across the sleeps between
    # polls, so a long wait cannot starve /v1/sync, link exchange or item removal.
    with runtime.session_factory() as session:
        with PLAID_WRITE_LOCK:
            item = None if body.new else find_sandbox_item(session)
            if item is not None:
                emit({"event": "reused", "item_id": item.id, "institution": item.institution_name})
            else:
                emit({"event": "linking", "username": username})
                item = link_sandbox_item(session, gateway, cipher, username)
                emit({"event": "linked", "item_id": item.id, "institution": item.institution_name})
            if not body.wait:
                r = sync_item(session, gateway, cipher, item)
                done = {
                    "added": r.added,
                    "status": r.status,
                    "history_complete": r.history_complete,
                    "timed_out": False,
                }
                emit({"event": "done", **done})
                return done

        def progress(n, r, elapsed):
            emit(
                {
                    "event": "poll",
                    "attempt": n,
                    "elapsed_s": round(elapsed, 1),
                    "added": r.added,
                    "status": r.status,
                    "history_complete": r.history_complete,
                }
            )

        emit({"event": "waiting", "interval_s": body.interval, "timeout_s": body.timeout})
        out = wait_for_history(
            session,
            gateway,
            cipher,
            item,
            timeout=body.timeout,
            interval=body.interval,
            on_poll=progress,
            sleep=sleep,
            clock=clock,
            guard=PLAID_WRITE_LOCK,
        )
    done = {
        "added": out.added,
        "status": out.status,
        "history_complete": out.history_complete,
        "timed_out": not out.history_complete,
        "polls": out.polls,
    }
    emit({"event": "done", **done})
    return done


# The job syncs as well as links, so starting one needs both permissions.
@router.post(
    "/sandbox-link",
    name="jobs.sandbox_link",
    status_code=202,
    dependencies=[require("link"), require("sync")],
)
def start_sandbox_link(body: SandboxLinkIn, state: StateDep, gateway: GatewayDep) -> dict[str, str]:
    runtime = state.runtime
    try:
        job_id = state.jobs.start(
            lambda emit: sandbox_link_job(
                runtime, gateway, body, emit, state.job_sleep, state.job_clock
            ),
            exclusive=SANDBOX_LINK_JOB,
        )
    except JobAlreadyRunning as exc:
        raise DaemonError(
            409,
            "JOB_ALREADY_RUNNING",
            "A sandbox-link job is already running; poll it instead of starting another.",
            job_id=exc.job_id,
        ) from None
    return {"job_id": job_id}


# Events hold only item ids, institution names, counts and statuses (and a Plaid error
# message), all readable through the read routes, so reading them needs only `read`.
@router.get("/{job_id}", name="jobs.get", dependencies=[require("read")])
def get_job(job_id: str, state: StateDep) -> JobOut:
    try:
        return state.jobs.get(job_id)
    except KeyError:
        raise DaemonError(404, "NOT_FOUND", "No such job.") from None
