"""Database work on several daemon threads at once: job syncs, audit writes and reads.

The workload runs in a child process, so a native crash (a connection closed under a running
statement) fails the test instead of killing pytest.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from sqlalchemy import func, select

from findata import queries
from findata.contexts import token_context
from findata.daemon.audit import AuditLog
from findata.db import AuditEntry, Item, Transaction, make_engine, make_session_factory
from findata.schema import ensure_current
from findata.security import Keys
from findata.sync import sync_item
from tests.conftest import account, txn

ROOT = Path(__file__).resolve().parent.parent
# Threads of each kind: syncing an Item (one each, so never two writers on one Item), writing
# audit rows, and reading.
THREADS = 3
ITERATIONS = 200


class _Gateway:
    """Each /transactions/sync adds one transaction; the access token names the Item."""

    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500) -> dict:
        item_id = access_token.removeprefix("token-")
        n = int(cursor or 0)
        return {
            "accounts": [account(f"{item_id}-acc")],
            "added": [txn(f"{item_id}-t{n}", account_id=f"{item_id}-acc")],
            "next_cursor": str(n + 1),
            "has_more": False,
            "transactions_update_status": "INITIAL_UPDATE_COMPLETE",
        }


def _workload(db_path: str) -> None:
    """Runs in the child: every thread does its work ITERATIONS times, then every write must
    be there. Exits non-zero with the errors (all synthetic data) if anything went wrong."""
    keys = Keys.from_master(b"k" * 32)
    engine = make_engine(db_path, keys.db_key)
    ensure_current(engine)
    factory = make_session_factory(engine)
    audit, gateway = AuditLog(factory), _Gateway()
    item_ids = [f"item_{n}" for n in range(THREADS)]
    with factory() as session:
        for item_id in item_ids:
            token = keys.cipher.encrypt(f"token-{item_id}", token_context(item_id))
            session.add(Item(id=item_id, access_token_enc=token))
        session.commit()

    def job_syncs(item_id: str) -> None:
        with factory() as session:  # like a sandbox-link job: one session for every pass
            item = session.get(Item, item_id)
            for _ in range(ITERATIONS):
                sync_item(session, gateway, keys.cipher, item)

    def audit_writes() -> None:
        for _ in range(ITERATIONS):
            audit.record("cli", "sync", "POST /v1/sync", "ok")

    def reads() -> None:
        for _ in range(ITERATIONS):
            with factory() as session:
                queries.list_transactions(session, limit=20)
            audit.recent(20)

    errors: list[str] = []

    def run(work, *args) -> None:
        try:
            work(*args)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    work = [(job_syncs, (i,)) for i in item_ids]
    work += [(audit_writes, ()), (reads, ())] * THREADS
    threads = [threading.Thread(target=run, args=(fn, *args)) for fn, args in work]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with factory() as session:
        audits = session.scalar(select(func.count()).select_from(AuditEntry))
        synced = session.scalar(select(func.count()).select_from(Transaction))
    expected = THREADS * ITERATIONS
    if (audits, synced) != (expected, expected):
        errors.append(f"lost writes: {audits} audit rows and {synced} transactions of {expected}")
    if errors:
        sys.exit("\n".join(errors))


def test_concurrent_job_commits_and_audit_writes_never_crash(tmp_path):
    code = "import sys\nfrom tests.test_db_concurrency import _workload\n_workload(sys.argv[1])\n"
    r = subprocess.run(  # noqa: S603 (our own interpreter and module)
        [sys.executable, "-X", "faulthandler", "-c", code, str(tmp_path / "findata.db")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "database is locked" not in r.stderr, r.stderr[-4000:]
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stderr[-4000:]}"


def test_file_databases_use_an_uncapped_queue_pool_and_memory_keeps_static(tmp_path):
    """Pins the pool choice above: the pysqlcipher default (SingletonThreadPool) closes other
    threads' connections mid-statement once more than five threads have connected."""
    from sqlalchemy.pool import QueuePool, StaticPool

    key = Keys.from_master(b"k" * 32).db_key
    engine = make_engine(tmp_path / "pool.db", key)
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool._max_overflow == -1  # no caller ever waits for a connection
    finally:
        engine.dispose()
    memory = make_engine(":memory:", key)
    try:
        assert isinstance(memory.pool, StaticPool)
    finally:
        memory.dispose()
