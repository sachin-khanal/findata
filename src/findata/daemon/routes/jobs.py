"""Long-running jobs: link a Plaid Sandbox bank and wait for its history, or wait for the
history of a bank already linked. Clients follow a job by id (`GET /v1/jobs/{id}/events`)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter
from sqlalchemy.orm import Session

from findata.daemon.deps import (
    PLAID_WRITE_LOCK,
    DaemonError,
    GatewayDep,
    SessionDep,
    StateDep,
    require,
)
from findata.daemon.events import EventBus
from findata.daemon.jobs import JobAlreadyRunning
from findata.daemon.routes.sync import publish_sync_completed
from findata.db import Item
from findata.linking import find_sandbox_item, link_sandbox_item
from findata.plaid_gateway import PlaidGateway
from findata.runtime import Runtime
from findata.security import FieldCipher
from findata.sync import sync_item
from findata.waiting import wait_for_history
from findata.wire import WAIT_INTERVAL_S, WAIT_TIMEOUT_S, HistoryJobIn, JobOut, SandboxLinkIn

DEFAULT_SANDBOX_USER = "user_transactions_dynamic"

router = APIRouter(prefix="/v1/jobs")

SANDBOX_LINK_JOB = "sandbox-link"
HISTORY_JOB = "history"
# One running job at a time across both kinds: each holds a worker thread and a database
# connection while it waits, and two of them would poll the same Item at Plaid twice as often.
PLAID_JOB = "plaid-job"

Emit = Callable[[dict], None]


def _wait_phase(
    session: Session,
    gateway: PlaidGateway,
    cipher: FieldCipher,
    events: EventBus,
    item: Item,
    emit: Emit,
    *,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], float] | None,
) -> dict:
    """Sync `item` until its history has loaded or `timeout` passes, emitting the `waiting`,
    `poll` and `done` events the CLI has always printed; returns the job's result.

    The write lock is held for each sync pass, never across the sleeps between passes, so a
    long wait cannot starve /v1/sync, link exchange or item removal.
    """

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
        publish_sync_completed(events, r)

    emit({"event": "waiting", "interval_s": interval, "timeout_s": timeout})
    out = wait_for_history(
        session,
        gateway,
        cipher,
        item,
        timeout=timeout,
        interval=interval,
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


def sandbox_link_job(
    runtime: Runtime,
    gateway: PlaidGateway,
    events: EventBus,
    body: SandboxLinkIn,
    emit: Emit,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], float] | None,
) -> dict:
    """The old `sandbox-link` CLI flow, with the same events; returns the job's result."""
    username = body.username or DEFAULT_SANDBOX_USER
    cipher = runtime.cipher
    with runtime.session_factory() as session:
        with PLAID_WRITE_LOCK:
            item = None if body.new else find_sandbox_item(session)
            if item is not None:
                emit({"event": "reused", "item_id": item.id, "institution": item.institution_name})
            else:
                emit({"event": "linking", "username": username})
                item = link_sandbox_item(session, gateway, cipher, username)
                emit({"event": "linked", "item_id": item.id, "institution": item.institution_name})
                events.publish("item_linked", item_id=item.id, institution=item.institution_name)
            if not body.wait:
                r = sync_item(session, gateway, cipher, item)
        if not body.wait:
            publish_sync_completed(events, r)
            done = {
                "added": r.added,
                "status": r.status,
                "history_complete": r.history_complete,
                "timed_out": False,
            }
            emit({"event": "done", **done})
            return done
        return _wait_phase(
            session,
            gateway,
            cipher,
            events,
            item,
            emit,
            timeout=body.timeout,
            interval=body.interval,
            sleep=sleep,
            clock=clock,
        )


def history_job(
    runtime: Runtime,
    gateway: PlaidGateway,
    events: EventBus,
    item_id: str,
    emit: Emit,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], float] | None,
) -> dict:
    """Wait for an already-linked Item's history, exactly as the sandbox-link job's wait."""
    with runtime.session_factory() as session:
        item = session.get(Item, item_id)
        if item is None:  # removed after the job was accepted: fails with the generic event
            raise LookupError("item removed before its history job started")
        return _wait_phase(
            session,
            gateway,
            runtime.cipher,
            events,
            item,
            emit,
            timeout=WAIT_TIMEOUT_S,
            interval=WAIT_INTERVAL_S,
            sleep=sleep,
            clock=clock,
        )


def _already_running(job_id: str) -> DaemonError:
    return DaemonError(
        409,
        "JOB_ALREADY_RUNNING",
        "A sandbox-link or history job is already running (one at a time); follow it "
        f"(GET /v1/jobs/{job_id}/events) instead of starting another.",
        job_id=job_id,
    )


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
                runtime, gateway, state.events, body, emit, state.job_sleep, state.job_clock
            ),
            exclusive=PLAID_JOB,
            labels={"kind": SANDBOX_LINK_JOB},
        )
    except JobAlreadyRunning as exc:
        raise _already_running(exc.job_id) from None
    return {"job_id": job_id}


@router.post("/history", name="jobs.history", status_code=202, dependencies=[require("sync")])
def start_history(
    body: HistoryJobIn, state: StateDep, gateway: GatewayDep, session: SessionDep
) -> dict[str, str]:
    if session.get(Item, body.item_id) is None:
        raise DaemonError(404, "NOT_FOUND", f"No linked item has id {body.item_id!r}.")
    runtime = state.runtime
    try:
        job_id = state.jobs.start(
            lambda emit: history_job(
                runtime, gateway, state.events, body.item_id, emit, state.job_sleep, state.job_clock
            ),
            exclusive=PLAID_JOB,
            labels={"kind": HISTORY_JOB, "item_id": body.item_id},
        )
    except JobAlreadyRunning as exc:
        raise _already_running(exc.job_id) from None
    return {"job_id": job_id}


# Events hold only item ids, institution names, counts and statuses (and a Plaid error
# message), all readable through the read routes, so reading them needs only `read`.
@router.get("/{job_id}", name="jobs.get", dependencies=[require("read")])
def get_job(job_id: str, state: StateDep) -> JobOut:
    try:
        return state.jobs.get(job_id)
    except KeyError:
        raise DaemonError(404, "NOT_FOUND", "No such job.") from None
