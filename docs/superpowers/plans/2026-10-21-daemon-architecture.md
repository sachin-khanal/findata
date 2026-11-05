# Daemon-centred Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `findata start` runs a headless daemon that alone holds the key, database and Plaid access; `findata serve web` and `findata serve mcp` are optional front ends, and the CLI, that get all data through it.

**Architecture:** The daemon exposes a versioned `/v1` HTTP API over an owner-only Unix socket. A typed `DaemonClient` is the only way the CLI, web server and MCP server reach data. Roles (`web`, `cli`, `mcp`) are enforced by the daemon; the `mcp` role is read-only unless a human grants time-limited write access. Every request is audited. An import-boundary test makes "all data through the daemon" enforceable.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (already used; uvicorn serves on a Unix socket), an HTTP client with Unix-socket transport (httpx, add as a runtime dependency; the project's dev group pins `httpx2`, so confirm which package the code should import), the official `mcp` Python SDK (FastMCP, stdio), Alembic (migration 0002), pytest with the existing fake Plaid gateway.

**Spec:** `docs/superpowers/specs/2026-10-21-daemon-architecture-design.md`

## Global Constraints

- Works on macOS, Linux and Windows (ADR 0002); macOS and Linux first, loopback with the same credentials is the acceptable Windows fallback.
- `--json` CLI output is preserved: one event per line, same event names and exit codes (including `sandbox-link` exit 2 on timeout).
- Existing `/api/*` data shapes served by the web server are preserved.
- The daemon refuses to start if the schema is behind (`SchemaOutOfDateError`), and only one daemon may run per data directory (exclusive lock).
- The daemon listens on an owner-only Unix socket inside the owner-only data directory. Not TCP, not reachable by other users.
- Front ends never open the database, load the master key or hold Plaid credentials. No silent fallback to direct DB access when the daemon is down.
- No transaction data or secrets in logs, the audit log, or error messages.
- Tests use the fake Plaid gateway and need no network.
- Core modules (`db`, `security`, `plaid_gateway`, `sync`, `linking`, `waiting`, `queries`, `schema`, `runtime`, `contexts`) are imported only by `daemon/` plus `init`, `migrate`, `start`.
- On macOS, creating a grant, approving an agent request, and removing an Item as `cli` or `web` need the user's presence (Touch ID or password), checked in the daemon (spec: "User presence for human-authority operations"; added 2026-11-04, Task 7b). Tests never show a real prompt: they inject a fake checker. No production code path, setting or environment variable switches the check off.
- No polling between findata's own processes and no sleep loops waiting inside the daemon: clients follow daemon event streams, and the daemon blocks on events and timers (spec: "Events, not polling"; added 2026-11-04, Task 7d). The one remaining poll is the bounded post-link wait on Plaid's history.

## Review Focus

The inputs the spec implies but a straight reading would not test, most likely first:

1. **Daemon crashed (kill -9) and left a socket file and lock behind:** `start` must succeed and not claim it is already running. Pinned in Task 5.
2. **Daemon not running:** every client command fails fast with `DAEMON_NOT_RUNNING` (missing socket and socket file with nothing listening), never a traceback or a hang. Pinned in Tasks 6 and 7.
3. **Credential file missing, empty, group-readable, or wrong token:** a clear error naming the file and the fix, and a 401 from the daemon surfaced as an authentication error. Pinned in Tasks 2 and 6.
4. **Grant edges:** exact-expiry instant, revoke while a write is in flight, daemon restart clears grants, grant for `sync` must not allow `items.remove`. Pinned in Tasks 3 and 10.
5. **Two syncs at once (CLI plus web, or two agents):** serialized in the daemon, never two writers on one Item. Pinned in Task 4.
6. **User presence:** a cancelled or timed-out prompt changes nothing; the prompt runs before the write lock is taken; the `mcp` role's own removal never prompts; a direct socket call with the `cli` credential still prompts. Pinned in Task 7b.
7. **Streams:** an idle stream holds no thread; a client that disconnects is noticed and frees its slot; a stream opened after events were published replays them without gaps or duplicates; events never carry transaction data or secrets. Pinned in Task 7d.
8. **Concurrent database work:** request handlers, job threads and audit writes running at once never crash the daemon or fail with "database is locked". Pinned in Task 7c.

---

## File Structure

| Path | Responsibility |
|---|---|
| `findata/privfs.py` (new, moved out of `security/files.py` and `security/token.py`) | Owner-only files/dirs, secret files, API token. No crypto, safe for front ends to import. |
| `findata/locations.py` (new) | `Locations(data_dir)`: paths of socket, lock, log, credentials dir; `ClientSettings` (data dir, web host/port only, no Plaid fields). |
| `findata/wire.py` (new, from `api/schemas.py`) | Pydantic request/response models shared by daemon and clients. |
| `findata/daemon/auth.py` | Roles, permissions, `CredentialStore`. |
| `findata/daemon/grants.py` | `GrantStore`, `ApprovalStore`. |
| `findata/daemon/audit.py` | `AuditLog` writing the `audit_log` table. |
| `findata/daemon/jobs.py` | `JobManager` for long operations (sandbox-link). |
| `findata/daemon/presence.py` (Task 7b) | `PresenceChecker`: Touch ID or password on macOS before human-authority operations. |
| `findata/daemon/events.py` (Task 7d) | `EventBus`: daemon events pushed to streaming clients. |
| `findata/daemon/scheduler.py` | `Scheduler`: the home for scheduled sync (no jobs registered yet). |
| `findata/daemon/app.py`, `daemon/routes/*.py` | The `/v1` API. |
| `findata/daemon/lock.py`, `daemon/server.py` | Single-instance lock; run, background spawn, stop. |
| `findata/client/__init__.py`, `client/errors.py` | `DaemonClient`, `DaemonNotRunning`, `DaemonRejected`. |
| `findata/web/` (rewritten; replaces `findata/api/`) | Web front end: forwards `/api/*` to the daemon; serves `index.html`. |
| `findata/mcp/server.py` | MCP front end. |
| `findata/cli/` | Data commands become client calls; new `start`, `stop`, `status`, `grant`, `revoke`, `approve`, `audit`, `serve web|mcp`. |
| `tests/test_boundary.py`, `tests/test_daemon_e2e.py` | Import boundary; real-socket end to end. |

---

### Task 1: Shared wire models, locations, and private-file helpers

**Files:**
- Create: `src/findata/wire.py`, `src/findata/locations.py`, `src/findata/privfs.py`
- Modify: `src/findata/security/files.py`, `src/findata/security/token.py` (become re-exports of `privfs`), `src/findata/api/schemas.py` (re-export from `wire` until Task 9 deletes `api/`)
- Test: `tests/test_locations.py`

**Interfaces:**
- Produces: `wire.ItemOut(id, institution_name, last_synced_at, accounts: int = 0, transactions: int = 0)`, `AccountOut`, `TransactionOut`, `ExchangeIn`, `SyncOut` (fields as today), `StatusOut(version, plaid_env, schema_revision, started_at, uptime_s)`, `GrantIn(operations: list[str], ttl_s: int)`, `GrantOut(operations: list[str], expires_at: datetime)`, `SandboxLinkIn(username, new, wait, timeout, interval)`, `JobOut(id, state: Literal["running","done","failed"], events: list[dict], result: dict | None)`. `privfs` exports the same names `security.files` had plus `load_or_create_api_token`. `Locations(data_dir: Path)` with properties `socket`, `lock`, `pid`, `log`, `credentials_dir`, `api_token`, `port_file`; `ClientSettings(findata_data_dir, findata_host, findata_port)` with `.locations`.

- [x] **Step 1: Write failing tests** in `tests/test_locations.py`: `test_locations_are_inside_the_data_dir` (every property's parent is `data_dir` or `data_dir/credentials`), `test_client_settings_ignore_plaid_secrets` (set `PLAID_SECRET` in env, assert `ClientSettings().model_dump()` has no key containing "plaid" or "secret"), `test_security_still_exports_the_private_file_helpers` (`from findata.security import ensure_private_dir, load_or_create_api_token` works).
- [x] **Step 2: Run** `uv run pytest tests/test_locations.py -v`; expected FAIL (modules missing).
- [x] **Step 3: Implement** by moving the owner-only file helpers and `load_or_create_api_token` verbatim into `privfs.py`, leaving `security/files.py` and `security/token.py` as thin re-exports; copy the models from `api/schemas.py` into `wire.py` and add the new ones; write `locations.py`.
- [x] **Step 4: Run** `uv run pytest -q`; expected: all existing tests plus the new ones pass.
- [x] **Step 5: Commit** `git commit -m "Extract wire models, locations and private-file helpers for front ends"`

**Done:** e79e2db.

### Task 2: Roles, permissions and credentials

**Files:**
- Create: `src/findata/daemon/__init__.py`, `src/findata/daemon/auth.py`
- Modify: `src/findata/cli/commands/setup.py` (`init` also calls `CredentialStore.ensure_all`)
- Test: `tests/test_daemon_auth.py`

**Interfaces:**
- Consumes: `privfs.load_or_create_secret`, `Locations`.
- Produces: `Role = Literal["web", "cli", "mcp"]`; `Permission = Literal["read","sync","link","items.remove","admin"]`; `ROLE_PERMISSIONS: dict[Role, frozenset[Permission]]` (`web` and `cli`: all; `mcp`: `{"read"}`); `class CredentialStore(locations: Locations)` with `ensure_all() -> None`, `role_for(token: str | None) -> Role | None` (constant-time compare).

- [x] **Step 1: Write failing tests:** `test_ensure_all_creates_one_owner_only_token_per_role` (three files, mode 0600, directory 0700, distinct values), `test_role_for_matches_each_token_and_rejects_others` (valid tokens map to their role; `None`, `""`, a token of the wrong length, and another install's token return `None`), `test_mcp_role_has_only_read_by_default`, `test_group_readable_credential_file_is_refused` (chmod 0644 then `role_for` raises `PermissionError` naming the file and `chmod 600`), `test_empty_credential_file_is_refused`.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_auth.py -v`; expected FAIL.
- [x] **Step 3: Implement** `auth.py`; credentials live in `locations.credentials_dir/<role>.token`, created with `secrets.token_urlsafe(32)`.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Add daemon roles, permissions and per-role credentials"`

**Done:** a1a32be (tests and code were written together, so Step 2's failing run was not captured).

### Task 3: Grants and approvals

**Files:**
- Create: `src/findata/daemon/grants.py`
- Test: `tests/test_daemon_grants.py`

**Interfaces:**
- Produces: `GRANTABLE = frozenset({"sync", "link", "items.remove"})`; `class GrantStore(clock: Callable[[], datetime])` with `grant(operations: set[str], ttl_s: int) -> GrantOut`, `revoke() -> None`, `allows(operation: str) -> bool`, `current() -> GrantOut | None`; `class ApprovalStore(clock, ttl_s: int = 300)` with `request(operation: str, subject: str) -> str` (returns request id), `approve(request_id: str) -> None`, `consume(request_id: str, operation: str, subject: str) -> bool` (true once, then false).

- [x] **Step 1: Write failing tests** (injected fake clock): `test_nothing_allowed_without_a_grant`, `test_grant_allows_only_named_operations` (grant `{"sync"}`: `allows("sync")`, not `allows("items.remove")`), `test_grant_expires_at_the_exact_instant` (allowed at ttl-1s, refused at ttl), `test_revoke_clears_immediately`, `test_new_grant_replaces_the_old_one`, `test_unknown_operation_cannot_be_granted` (`ValueError`), `test_approval_is_single_use`, `test_approval_is_bound_to_operation_and_subject` (an approval for item A cannot remove item B), `test_approval_expires`.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_grants.py -v`; expected FAIL.
- [x] **Step 3: Implement** in-memory stores (grants never persist, so a daemon restart clears them, as the spec requires); ids from `secrets.token_urlsafe(8)`.
- [x] **Step 4: Run** `uv run pytest tests/test_daemon_grants.py -v`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Add time-limited write grants and single-use approvals"`

**Done:** 37b89e9.

### Task 4: Audit log and the daemon API

**Files:**
- Create: `src/findata/migrations/versions/0002_audit_log.py`, `src/findata/daemon/audit.py`, `src/findata/daemon/app.py`, `src/findata/daemon/routes/{__init__,status,data,sync,link,items,grants,audit}.py`
- Modify: `src/findata/db.py` (add `AuditEntry` model, table `audit_log`: `id` autoincrement, `at` DateTime tz, `role` String(16), `operation` String(64), `summary` String(255), `outcome` String(32)), `src/findata/queries.py` (items with counts already exist; add nothing else)
- Test: `tests/test_daemon_api.py`, update `tests/test_schema.py` expectations only if the drift test needs the new head

**Interfaces:**
- Consumes: `Runtime`, `CredentialStore`, `GrantStore`, `ApprovalStore`, `queries`, `sync_all`, `link_item`, `remove_item`, `wire` models.
- Produces: `AuditLog(session_factory, clock)` with `record(role, operation, summary, outcome)` and `recent(limit) -> list[dict]`; `create_daemon_app(runtime: Runtime, credentials: CredentialStore, grants: GrantStore, approvals: ApprovalStore, audit: AuditLog, *, started_at: datetime, clock) -> FastAPI` exposing: `GET /v1/status`, `GET /v1/items` (with counts), `GET /v1/accounts`, `GET /v1/transactions` (same filters and limits as today), `POST /v1/sync`, `POST /v1/link/token`, `POST /v1/link/exchange`, `DELETE /v1/items/{item_id}?local_only=&request_id=`, `POST /v1/grants`, `GET /v1/grants`, `DELETE /v1/grants`, `GET /v1/approvals`, `POST /v1/approvals/{id}`, `GET /v1/audit?limit=`, `POST /v1/shutdown`. Errors are `{"error_code", "detail"}`: 401 `UNAUTHENTICATED`, 403 `FORBIDDEN`, 403 `WRITE_NOT_GRANTED` (detail tells the agent a human must run `findata grant mcp-write`), 403 `CONFIRMATION_REQUIRED` (plus `request_id`), 502 for Plaid errors (Plaid's `error_code`), 404 `NOT_FOUND`.
- Rule: `web`/`cli` pass every permission check; `mcp` passes `read`, and `sync`/`link`/`items.remove` only if `grants.allows(op)`; `items.remove` additionally needs `approvals.consume(request_id, "items.remove", item_id)`; without it the route creates a pending request and returns `CONFIRMATION_REQUIRED`. `admin` routes are refused for `mcp` regardless of grants. A process-wide lock serializes `/v1/sync`.

- [x] **Step 1: Write failing tests** with an in-process `TestClient` over `create_daemon_app` and the fake gateway: `test_no_token_is_401_and_unknown_token_is_401`, `test_read_routes_work_for_every_role`, `test_mcp_cannot_sync_without_grant_and_error_names_the_fix`, `test_mcp_can_sync_after_grant_then_not_after_expiry`, `test_grant_for_sync_does_not_allow_remove`, `test_mcp_cannot_use_admin_routes_even_with_a_grant`, `test_remove_by_agent_needs_a_human_approval_once` (first call `CONFIRMATION_REQUIRED` with `request_id`; `cli` role approves; retry succeeds; second retry with the same id fails), `test_every_request_is_audited_without_data` (after calls, `GET /v1/audit` rows have role/operation/outcome, and no transaction names, tokens or amounts appear in any row), `test_denied_requests_are_audited_too`, `test_concurrent_syncs_are_serialized` (two threads call `/v1/sync` against a gateway that records overlapping calls; assert never overlapped), `test_plaid_error_is_502_with_plaid_code`, plus migration test `test_migration_0002_creates_audit_log_and_matches_models` in `tests/test_schema.py`.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_api.py -v`; expected FAIL.
- [x] **Step 3: Implement** migration `0002` (batch-mode safe `create_table`), the model, `AuditLog`, and the app. Role resolution is one FastAPI dependency that reads `X-Findata-Credential`; audit is written by one middleware after each response using the route's operation name (route `name`) and a summary built from query-parameter *names*, never values.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS including the schema drift test.
- [x] **Step 5: Commit** `git commit -m "Add daemon /v1 API with role enforcement, grants and audit log"`

**Done:** 88c2a96, review fixes aef51e9.

### Task 5: Single-instance lock, server, scheduler hook, `start`/`stop`/`status`

**Files:**
- Create: `src/findata/daemon/lock.py`, `src/findata/daemon/server.py`, `src/findata/daemon/scheduler.py`, `src/findata/cli/commands/daemon.py`
- Modify: `src/findata/cli/__init__.py` (register `start`, `stop`, `status`)
- Test: `tests/test_daemon_server.py`

**Interfaces:**
- Produces: `DaemonLock(path: Path)` with `acquire() -> None` (raises `DaemonAlreadyRunning(pid: int | None)`) and `release()`, implemented with an OS advisory lock (released automatically when the process dies); `Scheduler` with `register(name: str, every_s: float, fn: Callable[[], None])`, `start()`, `stop()` (no jobs registered by default); `run_daemon(settings: Settings) -> None` (foreground: lock, `build_runtime`, credentials, create app, bind the Unix socket with mode 0600, serve, remove socket on exit); `findata start`, `findata stop` (calls `POST /v1/shutdown`), `findata status` (prints `StatusOut`).

- [x] **Step 1: Write failing tests:** `test_second_start_is_refused_with_the_running_pid`, `test_stale_socket_and_lock_after_a_crash_do_not_block_start` (leave a socket file and an unlocked lock file from a killed child process; `acquire()` succeeds and the stale socket is replaced), `test_start_refuses_when_schema_is_behind` (`SchemaOutOfDateError` surfaced as exit 1 with `findata migrate` in the message), `test_socket_is_owner_only` (mode 0600), `test_socket_is_removed_on_clean_shutdown`, `test_scheduler_runs_registered_job_and_stops` (short interval, fake job increments a counter), `test_stop_shuts_the_daemon_down` (start in a subprocess or thread with a temp data dir, `stop`, assert exit).
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_server.py -v`; expected FAIL.
- [x] **Step 3: Implement** lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), the server (uvicorn with `uds=`; on Windows bind `127.0.0.1` on a free port and write it to `Locations.port_file`), and the three commands. Scheduler starts and stops from the app's lifespan.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Add findata start/stop/status: lock, Unix-socket server, scheduler hook"`

**Done:** bd362dc builds `start` only; `stop` and `status` moved to Task 8 (ruling R2).

### Task 6: `DaemonClient`

**Files:**
- Create: `src/findata/client/__init__.py`, `src/findata/client/errors.py`
- Modify: `pyproject.toml` (HTTP client as a runtime dependency)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `Locations`, `wire`, privfs credential reader (owner-only check).
- Produces: `DaemonNotRunning(DaemonClientError)`, `DaemonRejected(error_code: str, detail: str, status: int, extra: dict)`; `DaemonClient(data_dir: Path, role: Role, *, timeout: float = 10.0)` with methods `status() -> StatusOut`, `items() -> list[ItemOut]`, `accounts() -> list[AccountOut]`, `transactions(*, start=None, end=None, account_id=None, category=None, limit=100, offset=0) -> list[TransactionOut]`, `sync() -> list[SyncOut]`, `link_token() -> str`, `exchange(public_token: str) -> ItemOut`, `remove_item(item_id: str, *, local_only=False, request_id=None) -> None`, `start_sandbox_link(body: SandboxLinkIn) -> str`, `job(job_id: str) -> JobOut`, `grant(operations, ttl_s) -> GrantOut`, `revoke()`, `approvals() -> list[dict]`, `approve(request_id)`, `audit(limit) -> list[dict]`, `shutdown()`.

- [x] **Step 1: Write failing tests** against an in-process daemon app mounted behind the client's transport: `test_client_round_trips_items_and_transactions`, `test_missing_socket_raises_daemon_not_running`, `test_socket_file_with_nothing_listening_raises_daemon_not_running` (not a hang; assert it returns within the timeout), `test_wrong_credential_raises_daemon_rejected_unauthenticated`, `test_missing_credential_file_names_the_file_and_fix`, `test_forbidden_write_surfaces_error_code_and_detail`.
- [x] **Step 2: Run** `uv run pytest tests/test_client.py -v`; expected FAIL.
- [x] **Step 3: Implement** `DaemonClient` as a thin typed wrapper; connection errors map to `DaemonNotRunning`; the credential for `role` is read from `Locations.credentials_dir` with the owner-only check.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Add typed DaemonClient with explicit not-running and rejected errors"`

**Done:** d22e2ae, review fixes 31c55c2.

### Task 7: Sandbox-link jobs in the daemon

**Files:**
- Create: `src/findata/daemon/jobs.py`, `src/findata/daemon/routes/jobs.py`
- Test: `tests/test_daemon_jobs.py`

**Interfaces:**
- Produces: `JobManager` with `start(fn: Callable[[Callable[[dict], None]], dict]) -> str` (runs `fn(emit)` on a worker thread; `emit` appends an event dict), `get(job_id: str) -> JobOut`; routes `POST /v1/jobs/sandbox-link` (permission `link`) and `GET /v1/jobs/{job_id}`. The job reuses `find_sandbox_item`, `link_sandbox_item`, `sync_item`, `wait_for_history` and emits exactly the events the CLI emits today (`reused`, `linking`, `linked`, `waiting`, `poll`, `done`, with the same fields); `result` carries `history_complete` and `timed_out`.

- [x] **Step 1: Write failing tests:** `test_job_emits_the_same_event_sequence_as_the_old_cli` (expected list `linking, linked, waiting, poll, poll, poll, poll, done` with `done` equal to the existing test's dict), `test_timeout_job_finishes_with_timed_out_true`, `test_plaid_error_fails_the_job_with_error_code`, `test_events_can_be_read_while_running` (poll `GET` between events), `test_mcp_role_cannot_start_a_job_without_a_grant`, `test_unknown_job_id_is_404`.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_jobs.py -v`; expected FAIL.
- [x] **Step 3: Implement** the manager and routes; reuse the fake clock approach from `tests/test_wait_and_cli.py` by passing `sleep`/`clock` into the job via the app factory.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Run sandbox-link as a daemon job with the existing event stream"`

**Done:** 310ff4a, review fixes b8e7790 (re-review clean).

### Task 7b: User presence (Touch ID) for human-authority operations

Added 2026-11-04 from the user's decision; spec section "User presence for human-authority operations". Placed before Task 8 so the CLI commands build on it; later task numbers are unchanged.

**Files:**
- Create: `src/findata/daemon/presence.py`
- Modify: `src/findata/daemon/{app.py,deps.py,server.py,audit.py}`, `src/findata/daemon/routes/{grants.py,items.py,status.py}`, `src/findata/wire.py`, `src/findata/client/__init__.py`, `pyproject.toml`, `tests/conftest.py`, the daemon-app fixtures in `tests/test_daemon_api.py` and `tests/test_daemon_jobs.py`
- Test: `tests/test_daemon_presence.py`

**Interfaces:**
- Produces: `Presence = Literal["confirmed", "denied", "not_possible", "unsupported"]`; a `PresenceChecker` protocol with `kind: Literal["touch_id", "unsupported"]` and `confirm(reason: str) -> Presence`; `MacPresence(timeout_s: float = 60.0, context_factory=None)` using `LocalAuthentication.LAContext` with `LAPolicyDeviceOwnerAuthentication` (Touch ID, or the login password), waiting on the reply callback with a `threading.Event`: success → `confirmed`; failure, cancel or timeout → `denied`; `canEvaluatePolicy` false → `not_possible`; `UnsupportedPresence` (always `unsupported`); `default_presence() -> PresenceChecker` (`MacPresence` when `sys.platform == "darwin"`, else `UnsupportedPresence`). `create_daemon_app(..., presence: PresenceChecker)` is a required keyword stored on `DaemonState`; `run_daemon(settings, ..., presence: PresenceChecker | None = None)` uses `default_presence()` when it is None.
- Gated, for the `cli` and `web` roles only: `POST /v1/grants` (reason "allow the findata agent to {operations joined with "and"} for {duration in minutes}"), `POST /v1/approvals/{id}` (reason "approve the agent's request to remove {institution}"), `DELETE /v1/items/{id}` (reason "remove {institution} and its local data"). The check runs after validation and the item lookup, before any change and before `PLAID_WRITE_LOCK` is taken. `denied` → 403 `USER_PRESENCE_REQUIRED` "Not confirmed at the Mac (Touch ID or password was cancelled or timed out). Nothing was changed."; `not_possible` → 403 `USER_PRESENCE_REQUIRED` "This needs confirmation at the Mac, and this session cannot show the Touch ID or password prompt (for example over SSH). Run it from a logged-in session on the Mac."; `confirmed` and `unsupported` proceed. Not gated: reads, sync, link, jobs, `DELETE /v1/grants` (revoke), status, shutdown, and the `mcp` role's own removal (the approval it consumes was confirmed in person).
- The audit summary of a gated request ends with ` presence=<outcome>`.
- `StatusOut.user_presence: Literal["touch_id", "unsupported"]`, from the checker's `kind`.
- `DaemonClient.grant()` and `approve()` use the long timeout, since the prompt waits up to 60 s.
- Dependency: `pyobjc-framework-LocalAuthentication; sys_platform == 'darwin'` (current release via `uv add`), imported only inside `MacPresence`.

- [x] **Step 1: Write failing tests** with a `FakePresence(outcome)` that records every reason it is asked to confirm: `test_grant_denied_stores_no_grant`, `test_grant_confirmed_creates_it_and_the_prompt_names_operations_and_duration`, `test_approve_denied_leaves_the_request_pending`, `test_cli_and_web_removal_denied_keeps_the_item_and_never_calls_plaid`, `test_agent_removal_with_an_approval_never_prompts`, `test_reads_sync_link_jobs_revoke_status_and_shutdown_never_prompt`, `test_not_possible_is_refused_with_the_ssh_hint`, `test_unsupported_platform_allows_and_audits_presence_unsupported`, `test_prompt_runs_before_the_write_lock_is_taken` (the fake asserts `not PLAID_WRITE_LOCK.locked()` while it is called), `test_prompt_reason_contains_no_credentials_or_tokens`, `test_status_reports_the_presence_kind`; `MacPresence` with a fake `context_factory`: success, failure, a reply that never arrives (→ `denied` after `timeout_s`), and `canEvaluatePolicy` false (→ `not_possible`). Add an autouse fixture in `tests/conftest.py` that makes `MacPresence.confirm` fail the test, so no test can ever show a real prompt; daemons started in subprocesses by tests must not call gated routes.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_presence.py -v`; expected FAIL.
- [x] **Step 3: Implement** `presence.py`, the gate as one dependency used by the three routes, the audit suffix, the status field and the client timeouts.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS. (The real prompt is checked by hand in Task 8, once `findata grant` exists.)
- [x] **Step 5: Commit** `git commit -m "Ask for Touch ID in the daemon before grants, approvals and removals"`

**Done:** 7aa35c2 (review approved; minors deferred to the final review; the real prompt is checked by hand in Task 8).

### Task 7c: Database access that concurrent daemon work cannot crash

Added 2026-11-04. Task 7b's implementer found an intermittent segfault in `tests/test_daemon_jobs.py` that is also present at ad8aa2f (3 crashes and 1 failure in 10 full runs): a sandbox-link job thread's SQLCipher commit (`sync_item`) running at the same moment as another request's audit insert (`AuditLog.record`). The daemon serves requests, jobs and audit writes on several threads, so this is a crash risk in real use, not only in tests. Placed before Task 7d, which adds more concurrency.

**Files:**
- Modify (expected; the root cause decides): `src/findata/db.py` and/or `src/findata/daemon/{deps.py,audit.py,routes/jobs.py}`
- Test: `tests/test_db_concurrency.py`

**Interfaces:**
- No new public interface is required. Keep: one writer per Item (`PLAID_WRITE_LOCK` semantics), an audit row for every request, and — if the fix serializes database access — no connection or transaction held across a Plaid network call or a job's sleep, so one slow Plaid call cannot stall every request.
- Root cause first (systematic debugging): reproduce with a minimal script (several threads, separate sessions, a file SQLCipher database from `make_engine`, concurrent commits) and capture the crashing threads' stacks with `faulthandler`. Write the cause and the evidence into the report before changing code. If the fault is inside sqlcipher3 or SQLCipher, check whether a current release fixes it; if none does, serialize database access within the process (for example, one pooled connection used by one thread at a time and released between transactions) instead of relying on per-thread connections.

- [x] **Step 1: Write a failing stress test** `tests/test_db_concurrency.py::test_concurrent_job_commits_and_audit_writes_never_crash` that runs the workload in a subprocess, so a segfault fails the test instead of killing pytest: several threads doing sync-style commits, audit inserts and reads on one file database for a fixed number of iterations; assert exit code 0 and no "database is locked" errors. If the fix serializes access, add `test_no_connection_is_held_during_plaid_calls_or_job_sleeps`.
- [x] **Step 2: Run** it; expected FAIL (crash) on the current code. If it does not crash reliably, raise the iteration count until it does and record the rate.
- [x] **Step 3: Fix** according to the root cause.
- [x] **Step 4: Run** `uv run pytest -q` 20 times in a row; expected: no crash and no failure in any run. Put the loop and its result in the report.
- [x] **Step 5: Commit** `git commit -m "Make concurrent database access in the daemon crash-free"`

**Done:** dd9ec8a. Root cause: the pysqlcipher dialect's default SingletonThreadPool closes connections other threads are using; file databases now use QueuePool. No serializing lock was needed, so the conditional test in Step 1 does not apply (ruling R26).

### Task 7d: Daemon event streams instead of polling

Added 2026-11-04 from the user's decision; spec section "Events, not polling". Placed after Task 7b and before Task 8, so the CLI, web and background start build on it.

**Files:**
- Create: `src/findata/daemon/events.py`, `src/findata/daemon/routes/events.py`
- Modify: `src/findata/daemon/{app.py,deps.py,jobs.py,grants.py}`, `src/findata/daemon/routes/{jobs.py,sync.py,link.py,items.py,grants.py}`, `src/findata/wire.py`, `src/findata/client/__init__.py`
- Test: `tests/test_daemon_events.py`, `tests/test_client_streams.py`

**Interfaces:**
- Produces: `EventBus(capacity: int = 256, max_subscribers: int = 32)` with `publish(event: str, **fields) -> int`, thread-safe and callable from worker threads, assigning a monotonically increasing `seq`; and an async subscription yielding events with `seq > after` that wakes only when something is published (bridged into the event loop with `loop.call_soon_threadsafe`; no sleep loop, no threadpool thread held while idle). A subscriber asking for a `seq` older than the buffer first gets `{"event": "gap"}` and should re-read state.
- `GET /v1/events?after=<seq>` (permission `read`): `application/x-ndjson`, one object per line `{"seq", "event", "at", ...fields}`, a `{"event": "keepalive"}` line after 15 s without events; it ends when the client disconnects or the daemon stops. More than `max_subscribers` open streams → 503 `TOO_MANY_STREAMS`.
- `GET /v1/jobs/{job_id}/events` (permission `read`): `application/x-ndjson`, the job's recorded events replayed first and then followed live, exactly the dicts the job records today (so the CLI's `--json` lines do not change), then `{"event": "job_finished", "state", "result"}`, then the stream ends. Keep-alive as above. `GET /v1/jobs/{job_id}` stays for one-off reads (MCP).
- `POST /v1/jobs/history` with `{"item_id"}` (permission `sync`): runs `wait_for_history` for an already-linked Item as a job (same lock-per-pass, timeout, interval and event names as the sandbox-link job's wait phase), under the same one-running-job rule: 409 `JOB_ALREADY_RUNNING` with the running `job_id`, so a client can follow that one instead.
- Daemon events (fields limited to ids, institution names, counts, statuses, operations and expiry times; never transaction data or secrets): `sync_completed` (per Item: `item_id`, `added`, `modified`, `removed`, `history_complete`), `item_linked`, `item_removed`, `job_started`, `job_finished`, `grant_created` (`operations`, `expires_at`), `grant_revoked`, `grant_expired` (published by a timer at the expiry instant), `approval_requested` (`request_id`, `operation`, `item_id`, `institution`), `approval_approved`, `approval_expired` (timer). Timers are injectable so tests never wait real time.
- Client: `DaemonClient.follow_job(job_id: str) -> Iterator[dict]` and `DaemonClient.events(after: int | None = None) -> Iterator[dict]`, reading the stream line by line with no read timeout between lines (the connect timeout still applies); keep-alive lines are skipped, not yielded; `DaemonNotRunning` and `DaemonRejected` as for other calls.

- [x] **Step 1: Write failing tests:** `test_job_stream_replays_then_follows_and_ends_with_job_finished` (events identical to `GET /v1/jobs/{id}`), `test_job_stream_keeps_the_cli_event_sequence` (`linking, linked, waiting, poll…, done`), `test_sync_publishes_sync_completed_per_item`, `test_grant_expired_is_published_by_a_timer_at_expiry`, `test_approval_requested_approved_and_expired_events`, `test_after_seq_resumes_without_duplicates`, `test_evicted_seq_yields_a_gap_event`, `test_too_many_streams_is_503`, `test_idle_streams_do_not_block_other_requests` (with several streams open and idle, ordinary requests still answer promptly), `test_disconnect_frees_the_subscriber_slot`, `test_history_job_waits_for_an_existing_item`, `test_history_job_409_names_the_running_job`, `test_events_never_contain_transaction_names_or_tokens`, `test_mcp_reads_events_but_needs_a_sync_grant_for_a_history_job`; client: `test_follow_job_yields_events_until_finished`, `test_events_iterator_skips_keepalives`, `test_streams_report_daemon_not_running`. If Starlette's `TestClient` cannot end an infinite stream cleanly, run those tests against a real server on a temporary Unix socket, as `tests/test_daemon_server.py` does.
- [x] **Step 2: Run** `uv run pytest tests/test_daemon_events.py tests/test_client_streams.py -v`; expected FAIL.
- [x] **Step 3: Implement** the bus, the two streaming routes, the history job, the event publication in the existing routes and stores, and the client iterators.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [x] **Step 5: Commit** `git commit -m "Push daemon events and job progress as streams instead of polling"`

**Done:** add2950, review fixes 11fb9cb (one running job across job kinds; the `mcp` role may hold at most 8 of the 32 streams, ruling R27).

### Task 8: CLI data commands become daemon clients

**Files:**
- Modify: `src/findata/cli/commands/{sync,txns,items,sandbox_link}.py`, `src/findata/cli/session.py`, `src/findata/cli/__init__.py`, `src/findata/cli/output.py`
- Create: `src/findata/cli/commands/{grants,audit}.py` (`grant`, `revoke`, `approve`, `audit`)
- Modify tests: `tests/test_wait_and_cli.py` (CLI tests now run against an in-process daemon)
- Test: `tests/test_cli_client.py`

**Interfaces:**
- Consumes: `DaemonClient`, `DaemonNotRunning`, `DaemonRejected`.
- Also (moved from Task 5, ruling R2): `findata stop` (`POST /v1/shutdown`) and `findata status` (prints `StatusOut`) in `cli/commands/daemon.py`, using only `DaemonClient`.
- Produces: `session.connect(as_json) -> DaemonClient` (role `cli`) replacing the runtime-based `connect`; a missing daemon exits 1 with `error_code: DAEMON_NOT_RUNNING` and the message "The findata daemon is not running. Start it with `findata start`." `findata grant mcp-write [--ops sync,items.remove] [--for 15m]`, `findata revoke mcp-write`, `findata approve <request-id>`, `findata audit [--limit N]`. `init` and `migrate` keep using the core directly; no other command may.
- User presence (Task 7b): `grant`, `approve` and `items rm` print "Confirm on your Mac (Touch ID or password)…" to stderr while the daemon waits (never into `--json` stdout), and map a `USER_PRESENCE_REQUIRED` refusal to exit 1 with that `error_code` and the daemon's message. The in-process daemon used by the CLI tests gets `FakePresence("confirmed")` unless a test says otherwise.
- Events (Task 7d): `sandbox-link` starts the job and prints its events from `client.follow_job()` as they arrive; no command re-reads a job on a timer.

- [x] **Step 1: Write failing tests:** every existing CLI test in `test_wait_and_cli.py` still passes unchanged in assertions when pointed at an in-process daemon (same event names, exit codes 0/1/2), `test_every_data_command_reports_daemon_not_running` (parametrized over `sync`, `txns`, `items list`, `items rm x`, `sandbox-link`, `audit`; exit 1, JSON `error_code == "DAEMON_NOT_RUNNING"`, no traceback), `test_grant_and_revoke_round_trip`, `test_approve_unblocks_an_agent_removal`, `test_presence_refusal_is_reported_clearly` (parametrized over `grant`, `approve`, `items rm`; exit 1, `error_code == "USER_PRESENCE_REQUIRED"`, nothing changed), `test_sandbox_link_follows_the_job_stream_without_polling` (the client's `job()` is never called).
- [x] **Step 2: Run** `uv run pytest tests/test_cli_client.py tests/test_wait_and_cli.py -v`; expected FAIL.
- [x] **Step 3: Implement** the commands against the client. Prefix matching for `items rm` happens client-side over `client.items()`. `sandbox-link` starts the job and prints events from `client.follow_job()` as they arrive, preserving exit codes.
- [x] **Step 4: Run** `uv run pytest -q`; expected PASS. Then, with the user watching, start the daemon in a visible terminal on a Sandbox data dir and run `findata grant mcp-write --for 1m` and `findata items rm` to confirm the Touch ID prompt appears with the right wording and that cancelling it refuses the operation.
- [x] **Step 5: Commit** `git commit -m "Route CLI data commands through the daemon; add grant/revoke/approve/audit"`

**Done:** 0158fed, review fix ee871b9 (one Plaid-rejected message in `output.py`). By hand on a Sandbox data dir: the Touch ID prompt appeared for `grant` and `items rm` and a fingerprint confirmed each; a declined prompt was checked by closing a real prompt from code (macOS replies not-approved, the daemon refuses), with the refusal itself covered by tests (ruling R29).

### Task 9: Web front end (`findata serve web`)

**Files:**
- Create: `src/findata/web/{app.py,deps.py,routes.py}`; `index.html` stays in `src/findata/web/`
- Delete: `src/findata/api/` (all of it), update `tests/test_api.py` to `tests/test_web.py`
- Modify: `src/findata/cli/__init__.py` (`serve` becomes a group: `serve web`; the old `serve` command is removed)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `DaemonClient` (role `web`), `privfs.load_or_create_api_token`, `ClientSettings`.
- Produces: `create_web_app(client: DaemonClient, api_token: str) -> FastAPI` serving `/`, `/favicon.ico`, `/api/health`, `/api/items`, `/api/accounts`, `/api/transactions`, `/api/sync`, `/api/link/token`, `/api/link/exchange`, `/docs`, with the same auth header, Host allow-list and response shapes as today. `findata serve web` runs it on `findata_host:findata_port`.
- Events (Task 7d): `GET /api/events` relays `client.events()` to the page as NDJSON; it needs the token header like every data route, so the page reads it with `fetch` and a `ReadableStream` (not `EventSource`, which cannot send the header). After `/api/link/exchange` the web server starts a history job for the new Item. `index.html` drops its `MAX_POLLS`/`POLL_MS` retry loop: Sync posts once and the page updates from `sync_completed` and job events; if history is still loading it says so and starts (or follows) a history job.

- [ ] **Step 1: Write failing tests:** port every case in the current `tests/test_api.py` to run against a web app whose client talks to an in-process daemon (same assertions: 401 without token, Host spoof 400, filters, sync result shape, 502 for Plaid errors); add `test_web_returns_503_with_start_hint_when_daemon_is_down` and `test_web_app_never_imports_core_modules` (assert none of the core modules are in `sys.modules` after importing `findata.web.app` in a fresh subprocess), `test_web_events_relays_daemon_events_and_needs_the_token`, `test_link_exchange_starts_a_history_job`, `test_page_has_no_retry_loop` (no `POLL_MS`/`MAX_POLLS`, no timer-driven re-requests).
- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -v`; expected FAIL.
- [ ] **Step 3: Implement** the forwarding app: each route calls the matching client method; `DaemonRejected` becomes the same status and JSON body; `DaemonNotRunning` becomes 503 with `error_code: DAEMON_NOT_RUNNING`. The page's status line shows that state instead of a raw error.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS. Then restart the web server in a visible terminal and open the UI in the browser to confirm data loads through the daemon.
- [ ] **Step 5: Commit** `git commit -m "Replace in-process API with a web front end that talks to the daemon"`

### Task 10: MCP front end, read tools, and grant-gated write tools

**Files:**
- Create: `src/findata/mcp/{__init__,server}.py`
- Modify: `src/findata/cli/__init__.py` (`serve mcp`), `pyproject.toml` (`mcp` dependency)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `DaemonClient` (role `mcp`).
- Produces: `build_mcp_server(client: DaemonClient) -> FastMCP` with read tools `get_status()`, `list_items()`, `list_accounts()`, `list_transactions(start=None, end=None, account_id=None, category=None, limit=100, offset=0)`, and write tools `sync()`, `remove_item(item_id: str, request_id: str | None = None)`; each write tool's description states that it needs a human grant. `findata serve mcp` runs it over stdio.

- [ ] **Step 1: Write failing tests** with the MCP SDK's in-memory client session against an in-process daemon: `test_read_tools_return_daemon_data`, `test_write_tool_without_grant_returns_a_clear_human_must_authorize_error`, `test_write_tool_succeeds_after_grant_and_fails_after_expiry_and_revoke`, `test_remove_item_returns_request_id_then_needs_human_approval`, `test_tools_report_daemon_not_running_clearly`, `test_mcp_module_never_imports_core_modules` (subprocess import check as in Task 9), `test_tool_list_marks_write_tools_as_requiring_a_grant`.
- [ ] **Step 2: Run** `uv run pytest tests/test_mcp.py -v`; expected FAIL.
- [ ] **Step 3: Implement** with the official SDK (confirm its current FastMCP API against its docs); map `DaemonRejected(WRITE_NOT_GRANTED)` to a tool error whose message tells the agent to ask the user to run `findata grant mcp-write`.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add MCP front end: read tools and grant-gated write tools"`

### Task 11: Web approval panel

**Files:**
- Modify: `src/findata/web/routes.py` (`GET/POST /api/grants`, `GET /api/approvals`, `POST /api/approvals/{id}`), `src/findata/web/index.html`
- Test: `tests/test_web.py` (add cases)

**Interfaces:**
- Consumes: `DaemonClient.grant/revoke/approvals/approve`.
- Produces: a small "Agent access" section in the UI: current grant with expiry and a Revoke button, a "Grant agent writes" button (choose operations and duration), and a list of pending removal requests with Approve.
- User presence (Task 7b): Grant and Approve bring up the Touch ID prompt on the Mac (the daemon asks, not the page). The UI shows "Confirm on your Mac…" while the request is pending and shows a `USER_PRESENCE_REQUIRED` refusal as a plain message.
- Events (Task 7d): the section updates live from `grant_*` and `approval_*` events; it never re-fetches on a timer.

- [ ] **Step 1: Write failing tests:** `test_web_grant_round_trip`, `test_web_lists_and_approves_pending_removals`, `test_web_grant_requires_the_browser_token`, `test_web_reports_a_presence_refusal`, `test_panel_updates_from_events` (an `approval_requested` event shows the request without a reload).
- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -v`; expected FAIL.
- [ ] **Step 3: Implement** the routes and the UI section, matching the existing page's plain style.
- [ ] **Step 4: Run** `uv run pytest -q`, then check the panel in the browser; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add agent-access panel to the web UI"`

### Task 12: `start --background`

**Files:**
- Modify: `src/findata/daemon/server.py`, `src/findata/cli/commands/daemon.py`
- Test: `tests/test_daemon_background.py`

**Interfaces:**
- Produces: `findata start --background` spawns the daemon detached (new session, stdout/stderr to `Locations.log`), waits for a ready signal from the child (the parent passes the write end of a pipe with `pass_fds`, the child writes one line once its listener is bound and serving, and the parent blocks on the read end with a timeout; EOF before the line means the child died), then exits 0 printing the pid and log path; if the child exits first or the wait times out it prints the reason (last log lines) and exits 1.

- [ ] **Step 1: Write failing tests:** `test_background_start_returns_when_the_daemon_is_ready`, `test_background_start_fails_loudly_if_the_child_dies` (force a schema-behind database; exit 1, message includes `findata migrate`, no stray process), `test_background_daemon_logs_to_file_and_not_the_terminal`, `test_status_and_stop_work_on_a_backgrounded_daemon`, `test_log_file_is_owner_only_and_contains_no_credentials`, `test_background_start_waits_on_the_ready_signal` (the parent makes no status requests while waiting).
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_background.py -v`; expected FAIL.
- [ ] **Step 3: Implement** with `subprocess.Popen(..., start_new_session=True)` re-invoking `python -m findata start`; readiness comes from the pipe, never from polling the client.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add start --background"`

### Task 13: Boundary test, end-to-end test, docs

**Files:**
- Create: `tests/test_boundary.py`, `tests/test_daemon_e2e.py`, `docs/adr/0004-daemon-centred-architecture.md`
- Modify: `README.md` (quickstart: `findata start`, `serve web`, `serve mcp`, grant flow), `docs/DEV_SETUP.txt`, roadmap

**Interfaces:** none new.

- [ ] **Step 1: Write the tests:** `test_boundary.py::test_front_ends_and_data_cli_never_import_core_modules` parses every file under `findata/web`, `findata/mcp`, `findata/client` and `findata/cli/commands/{sync,txns,items,sandbox_link,grants,audit}.py` (`commands/daemon.py` is excluded because `start` runs the core; keep `stop` and `status` there using only `DaemonClient`)` and fails on any import of `findata.db`, `findata.security`, `findata.plaid_gateway`, `findata.sync`, `findata.linking`, `findata.waiting`, `findata.queries`, `findata.schema`, `findata.runtime`, `findata.contexts`, `findata.config`; `test_daemon_e2e.py::test_real_socket_end_to_end` starts a daemon on a temp data dir with the fake gateway, drives it with `DaemonClient` as `cli`, `web`, `mcp`, and asserts: read works for all, `mcp` write is refused, works after `grant`, refused after `revoke`, and a wrong credential is rejected. The daemon runs in-process (a thread calling `run_daemon(..., presence=FakePresence("confirmed"))`), so no real prompt appears. It also opens `GET /v1/events` as `web` and sees `sync_completed` after a sync.
- [ ] **Step 2: Run** `uv run pytest tests/test_boundary.py tests/test_daemon_e2e.py -v`; expected PASS (they should already hold; if the boundary test fails, fix the import it names).
- [ ] **Step 3: Write the docs:** ADR 0004 recording the decisions in the spec, including user presence and its residual limits (it stops an agent using the CLI or socket with human authority; it does not stop a process that can change findata's code, restart the daemon or read the keychain, so such agents must be sandboxed); README quickstart updated, with an "Agent access" section on grant, approve and Touch ID; DEV_SETUP describes the process model, credentials, grants, and how to run daemon plus front ends in separate terminals.
- [ ] **Step 4: Run** `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; expected all pass.
- [ ] **Step 5: Commit** `git commit -m "Enforce the daemon boundary; end-to-end test; ADR 0004 and docs"`

---

## Self-review notes

- Spec coverage: command surface (Tasks 5, 8, 9, 10, 12), daemon duties and lock (5), audit (4), roles and credentials (2), grants and user-present write (3, 4, 8, 11), web front end (9), MCP (10), client and `DAEMON_NOT_RUNNING` (6, 8), boundary enforcement (13), scheduler hook (5), foreground default and `--background` (5, 12), Windows fallback (5).
- Calls the plan makes beyond the spec: the grant mechanism (CLI `grant`/`revoke`/`approve` and the web panel) is the spec's marked guess; the `link` operation is grantable but the sandbox-link job is the only agent-triggerable link path; `findata audit` and `GET /v1/audit` exist so the audit log can be read.
