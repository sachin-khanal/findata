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

## Review Focus

The inputs the spec implies but a straight reading would not test, most likely first:

1. **Daemon crashed (kill -9) and left a socket file and lock behind:** `start` must succeed and not claim it is already running. Pinned in Task 5.
2. **Daemon not running:** every client command fails fast with `DAEMON_NOT_RUNNING` (missing socket and socket file with nothing listening), never a traceback or a hang. Pinned in Tasks 6 and 7.
3. **Credential file missing, empty, group-readable, or wrong token:** a clear error naming the file and the fix, and a 401 from the daemon surfaced as an authentication error. Pinned in Tasks 2 and 6.
4. **Grant edges:** exact-expiry instant, revoke while a write is in flight, daemon restart clears grants, grant for `sync` must not allow `items.remove`. Pinned in Tasks 3 and 10.
5. **Two syncs at once (CLI plus web, or two agents):** serialized in the daemon, never two writers on one Item. Pinned in Task 4.

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

- [ ] **Step 1: Write failing tests** in `tests/test_locations.py`: `test_locations_are_inside_the_data_dir` (every property's parent is `data_dir` or `data_dir/credentials`), `test_client_settings_ignore_plaid_secrets` (set `PLAID_SECRET` in env, assert `ClientSettings().model_dump()` has no key containing "plaid" or "secret"), `test_security_still_exports_the_private_file_helpers` (`from findata.security import ensure_private_dir, load_or_create_api_token` works).
- [ ] **Step 2: Run** `uv run pytest tests/test_locations.py -v`; expected FAIL (modules missing).
- [ ] **Step 3: Implement** by moving the owner-only file helpers and `load_or_create_api_token` verbatim into `privfs.py`, leaving `security/files.py` and `security/token.py` as thin re-exports; copy the models from `api/schemas.py` into `wire.py` and add the new ones; write `locations.py`.
- [ ] **Step 4: Run** `uv run pytest -q`; expected: all existing tests plus the new ones pass.
- [ ] **Step 5: Commit** `git commit -m "Extract wire models, locations and private-file helpers for front ends"`

### Task 2: Roles, permissions and credentials

**Files:**
- Create: `src/findata/daemon/__init__.py`, `src/findata/daemon/auth.py`
- Modify: `src/findata/cli/commands/setup.py` (`init` also calls `CredentialStore.ensure_all`)
- Test: `tests/test_daemon_auth.py`

**Interfaces:**
- Consumes: `privfs.load_or_create_secret`, `Locations`.
- Produces: `Role = Literal["web", "cli", "mcp"]`; `Permission = Literal["read","sync","link","items.remove","admin"]`; `ROLE_PERMISSIONS: dict[Role, frozenset[Permission]]` (`web` and `cli`: all; `mcp`: `{"read"}`); `class CredentialStore(locations: Locations)` with `ensure_all() -> None`, `role_for(token: str | None) -> Role | None` (constant-time compare).

- [ ] **Step 1: Write failing tests:** `test_ensure_all_creates_one_owner_only_token_per_role` (three files, mode 0600, directory 0700, distinct values), `test_role_for_matches_each_token_and_rejects_others` (valid tokens map to their role; `None`, `""`, a token of the wrong length, and another install's token return `None`), `test_mcp_role_has_only_read_by_default`, `test_group_readable_credential_file_is_refused` (chmod 0644 then `role_for` raises `PermissionError` naming the file and `chmod 600`), `test_empty_credential_file_is_refused`.
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_auth.py -v`; expected FAIL.
- [ ] **Step 3: Implement** `auth.py`; credentials live in `locations.credentials_dir/<role>.token`, created with `secrets.token_urlsafe(32)`.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add daemon roles, permissions and per-role credentials"`

### Task 3: Grants and approvals

**Files:**
- Create: `src/findata/daemon/grants.py`
- Test: `tests/test_daemon_grants.py`

**Interfaces:**
- Produces: `GRANTABLE = frozenset({"sync", "link", "items.remove"})`; `class GrantStore(clock: Callable[[], datetime])` with `grant(operations: set[str], ttl_s: int) -> GrantOut`, `revoke() -> None`, `allows(operation: str) -> bool`, `current() -> GrantOut | None`; `class ApprovalStore(clock, ttl_s: int = 300)` with `request(operation: str, subject: str) -> str` (returns request id), `approve(request_id: str) -> None`, `consume(request_id: str, operation: str, subject: str) -> bool` (true once, then false).

- [ ] **Step 1: Write failing tests** (injected fake clock): `test_nothing_allowed_without_a_grant`, `test_grant_allows_only_named_operations` (grant `{"sync"}`: `allows("sync")`, not `allows("items.remove")`), `test_grant_expires_at_the_exact_instant` (allowed at ttl-1s, refused at ttl), `test_revoke_clears_immediately`, `test_new_grant_replaces_the_old_one`, `test_unknown_operation_cannot_be_granted` (`ValueError`), `test_approval_is_single_use`, `test_approval_is_bound_to_operation_and_subject` (an approval for item A cannot remove item B), `test_approval_expires`.
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_grants.py -v`; expected FAIL.
- [ ] **Step 3: Implement** in-memory stores (grants never persist, so a daemon restart clears them, as the spec requires); ids from `secrets.token_urlsafe(8)`.
- [ ] **Step 4: Run** `uv run pytest tests/test_daemon_grants.py -v`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add time-limited write grants and single-use approvals"`

### Task 4: Audit log and the daemon API

**Files:**
- Create: `src/findata/migrations/versions/0002_audit_log.py`, `src/findata/daemon/audit.py`, `src/findata/daemon/app.py`, `src/findata/daemon/routes/{__init__,status,data,sync,link,items,grants,audit}.py`
- Modify: `src/findata/db.py` (add `AuditEntry` model, table `audit_log`: `id` autoincrement, `at` DateTime tz, `role` String(16), `operation` String(64), `summary` String(255), `outcome` String(32)), `src/findata/queries.py` (items with counts already exist; add nothing else)
- Test: `tests/test_daemon_api.py`, update `tests/test_schema.py` expectations only if the drift test needs the new head

**Interfaces:**
- Consumes: `Runtime`, `CredentialStore`, `GrantStore`, `ApprovalStore`, `queries`, `sync_all`, `link_item`, `remove_item`, `wire` models.
- Produces: `AuditLog(session_factory, clock)` with `record(role, operation, summary, outcome)` and `recent(limit) -> list[dict]`; `create_daemon_app(runtime: Runtime, credentials: CredentialStore, grants: GrantStore, approvals: ApprovalStore, audit: AuditLog, *, started_at: datetime, clock) -> FastAPI` exposing: `GET /v1/status`, `GET /v1/items` (with counts), `GET /v1/accounts`, `GET /v1/transactions` (same filters and limits as today), `POST /v1/sync`, `POST /v1/link/token`, `POST /v1/link/exchange`, `DELETE /v1/items/{item_id}?local_only=&request_id=`, `POST /v1/grants`, `GET /v1/grants`, `DELETE /v1/grants`, `GET /v1/approvals`, `POST /v1/approvals/{id}`, `GET /v1/audit?limit=`, `POST /v1/shutdown`. Errors are `{"error_code", "detail"}`: 401 `UNAUTHENTICATED`, 403 `FORBIDDEN`, 403 `WRITE_NOT_GRANTED` (detail tells the agent a human must run `findata grant mcp-write`), 403 `CONFIRMATION_REQUIRED` (plus `request_id`), 502 for Plaid errors (Plaid's `error_code`), 404 `NOT_FOUND`.
- Rule: `web`/`cli` pass every permission check; `mcp` passes `read`, and `sync`/`link`/`items.remove` only if `grants.allows(op)`; `items.remove` additionally needs `approvals.consume(request_id, "items.remove", item_id)`; without it the route creates a pending request and returns `CONFIRMATION_REQUIRED`. `admin` routes are refused for `mcp` regardless of grants. A process-wide lock serializes `/v1/sync`.

- [ ] **Step 1: Write failing tests** with an in-process `TestClient` over `create_daemon_app` and the fake gateway: `test_no_token_is_401_and_unknown_token_is_401`, `test_read_routes_work_for_every_role`, `test_mcp_cannot_sync_without_grant_and_error_names_the_fix`, `test_mcp_can_sync_after_grant_then_not_after_expiry`, `test_grant_for_sync_does_not_allow_remove`, `test_mcp_cannot_use_admin_routes_even_with_a_grant`, `test_remove_by_agent_needs_a_human_approval_once` (first call `CONFIRMATION_REQUIRED` with `request_id`; `cli` role approves; retry succeeds; second retry with the same id fails), `test_every_request_is_audited_without_data` (after calls, `GET /v1/audit` rows have role/operation/outcome, and no transaction names, tokens or amounts appear in any row), `test_denied_requests_are_audited_too`, `test_concurrent_syncs_are_serialized` (two threads call `/v1/sync` against a gateway that records overlapping calls; assert never overlapped), `test_plaid_error_is_502_with_plaid_code`, plus migration test `test_migration_0002_creates_audit_log_and_matches_models` in `tests/test_schema.py`.
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_api.py -v`; expected FAIL.
- [ ] **Step 3: Implement** migration `0002` (batch-mode safe `create_table`), the model, `AuditLog`, and the app. Role resolution is one FastAPI dependency that reads `X-Findata-Credential`; audit is written by one middleware after each response using the route's operation name (route `name`) and a summary built from query-parameter *names*, never values.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS including the schema drift test.
- [ ] **Step 5: Commit** `git commit -m "Add daemon /v1 API with role enforcement, grants and audit log"`

### Task 5: Single-instance lock, server, scheduler hook, `start`/`stop`/`status`

**Files:**
- Create: `src/findata/daemon/lock.py`, `src/findata/daemon/server.py`, `src/findata/daemon/scheduler.py`, `src/findata/cli/commands/daemon.py`
- Modify: `src/findata/cli/__init__.py` (register `start`, `stop`, `status`)
- Test: `tests/test_daemon_server.py`

**Interfaces:**
- Produces: `DaemonLock(path: Path)` with `acquire() -> None` (raises `DaemonAlreadyRunning(pid: int | None)`) and `release()`, implemented with an OS advisory lock (released automatically when the process dies); `Scheduler` with `register(name: str, every_s: float, fn: Callable[[], None])`, `start()`, `stop()` (no jobs registered by default); `run_daemon(settings: Settings) -> None` (foreground: lock, `build_runtime`, credentials, create app, bind the Unix socket with mode 0600, serve, remove socket on exit); `findata start`, `findata stop` (calls `POST /v1/shutdown`), `findata status` (prints `StatusOut`).

- [ ] **Step 1: Write failing tests:** `test_second_start_is_refused_with_the_running_pid`, `test_stale_socket_and_lock_after_a_crash_do_not_block_start` (leave a socket file and an unlocked lock file from a killed child process; `acquire()` succeeds and the stale socket is replaced), `test_start_refuses_when_schema_is_behind` (`SchemaOutOfDateError` surfaced as exit 1 with `findata migrate` in the message), `test_socket_is_owner_only` (mode 0600), `test_socket_is_removed_on_clean_shutdown`, `test_scheduler_runs_registered_job_and_stops` (short interval, fake job increments a counter), `test_stop_shuts_the_daemon_down` (start in a subprocess or thread with a temp data dir, `stop`, assert exit).
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_server.py -v`; expected FAIL.
- [ ] **Step 3: Implement** lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), the server (uvicorn with `uds=`; on Windows bind `127.0.0.1` on a free port and write it to `Locations.port_file`), and the three commands. Scheduler starts and stops from the app's lifespan.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add findata start/stop/status: lock, Unix-socket server, scheduler hook"`

### Task 6: `DaemonClient`

**Files:**
- Create: `src/findata/client/__init__.py`, `src/findata/client/errors.py`
- Modify: `pyproject.toml` (HTTP client as a runtime dependency)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `Locations`, `wire`, privfs credential reader (owner-only check).
- Produces: `DaemonNotRunning(DaemonClientError)`, `DaemonRejected(error_code: str, detail: str, status: int, extra: dict)`; `DaemonClient(data_dir: Path, role: Role, *, timeout: float = 10.0)` with methods `status() -> StatusOut`, `items() -> list[ItemOut]`, `accounts() -> list[AccountOut]`, `transactions(*, start=None, end=None, account_id=None, category=None, limit=100, offset=0) -> list[TransactionOut]`, `sync() -> list[SyncOut]`, `link_token() -> str`, `exchange(public_token: str) -> ItemOut`, `remove_item(item_id: str, *, local_only=False, request_id=None) -> None`, `start_sandbox_link(body: SandboxLinkIn) -> str`, `job(job_id: str) -> JobOut`, `grant(operations, ttl_s) -> GrantOut`, `revoke()`, `approvals() -> list[dict]`, `approve(request_id)`, `audit(limit) -> list[dict]`, `shutdown()`.

- [ ] **Step 1: Write failing tests** against an in-process daemon app mounted behind the client's transport: `test_client_round_trips_items_and_transactions`, `test_missing_socket_raises_daemon_not_running`, `test_socket_file_with_nothing_listening_raises_daemon_not_running` (not a hang; assert it returns within the timeout), `test_wrong_credential_raises_daemon_rejected_unauthenticated`, `test_missing_credential_file_names_the_file_and_fix`, `test_forbidden_write_surfaces_error_code_and_detail`.
- [ ] **Step 2: Run** `uv run pytest tests/test_client.py -v`; expected FAIL.
- [ ] **Step 3: Implement** `DaemonClient` as a thin typed wrapper; connection errors map to `DaemonNotRunning`; the credential for `role` is read from `Locations.credentials_dir` with the owner-only check.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add typed DaemonClient with explicit not-running and rejected errors"`

### Task 7: Sandbox-link jobs in the daemon

**Files:**
- Create: `src/findata/daemon/jobs.py`, `src/findata/daemon/routes/jobs.py`
- Test: `tests/test_daemon_jobs.py`

**Interfaces:**
- Produces: `JobManager` with `start(fn: Callable[[Callable[[dict], None]], dict]) -> str` (runs `fn(emit)` on a worker thread; `emit` appends an event dict), `get(job_id: str) -> JobOut`; routes `POST /v1/jobs/sandbox-link` (permission `link`) and `GET /v1/jobs/{job_id}`. The job reuses `find_sandbox_item`, `link_sandbox_item`, `sync_item`, `wait_for_history` and emits exactly the events the CLI emits today (`reused`, `linking`, `linked`, `waiting`, `poll`, `done`, with the same fields); `result` carries `history_complete` and `timed_out`.

- [ ] **Step 1: Write failing tests:** `test_job_emits_the_same_event_sequence_as_the_old_cli` (expected list `linking, linked, waiting, poll, poll, poll, poll, done` with `done` equal to the existing test's dict), `test_timeout_job_finishes_with_timed_out_true`, `test_plaid_error_fails_the_job_with_error_code`, `test_events_can_be_read_while_running` (poll `GET` between events), `test_mcp_role_cannot_start_a_job_without_a_grant`, `test_unknown_job_id_is_404`.
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_jobs.py -v`; expected FAIL.
- [ ] **Step 3: Implement** the manager and routes; reuse the fake clock approach from `tests/test_wait_and_cli.py` by passing `sleep`/`clock` into the job via the app factory.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Run sandbox-link as a daemon job with the existing event stream"`

### Task 8: CLI data commands become daemon clients

**Files:**
- Modify: `src/findata/cli/commands/{sync,txns,items,sandbox_link}.py`, `src/findata/cli/session.py`, `src/findata/cli/__init__.py`, `src/findata/cli/output.py`
- Create: `src/findata/cli/commands/{grants,audit}.py` (`grant`, `revoke`, `approve`, `audit`)
- Modify tests: `tests/test_wait_and_cli.py` (CLI tests now run against an in-process daemon)
- Test: `tests/test_cli_client.py`

**Interfaces:**
- Consumes: `DaemonClient`, `DaemonNotRunning`, `DaemonRejected`.
- Produces: `session.connect(as_json) -> DaemonClient` (role `cli`) replacing the runtime-based `connect`; a missing daemon exits 1 with `error_code: DAEMON_NOT_RUNNING` and the message "The findata daemon is not running. Start it with `findata start`." `findata grant mcp-write [--ops sync,items.remove] [--for 15m]`, `findata revoke mcp-write`, `findata approve <request-id>`, `findata audit [--limit N]`. `init` and `migrate` keep using the core directly; no other command may.

- [ ] **Step 1: Write failing tests:** every existing CLI test in `test_wait_and_cli.py` still passes unchanged in assertions when pointed at an in-process daemon (same event names, exit codes 0/1/2), `test_every_data_command_reports_daemon_not_running` (parametrized over `sync`, `txns`, `items list`, `items rm x`, `sandbox-link`, `audit`; exit 1, JSON `error_code == "DAEMON_NOT_RUNNING"`, no traceback), `test_grant_and_revoke_round_trip`, `test_approve_unblocks_an_agent_removal`.
- [ ] **Step 2: Run** `uv run pytest tests/test_cli_client.py tests/test_wait_and_cli.py -v`; expected FAIL.
- [ ] **Step 3: Implement** the commands against the client. Prefix matching for `items rm` happens client-side over `client.items()`. `sandbox-link` starts the job and prints events as `job()` returns them, preserving exit codes.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Route CLI data commands through the daemon; add grant/revoke/approve/audit"`

### Task 9: Web front end (`findata serve web`)

**Files:**
- Create: `src/findata/web/{app.py,deps.py,routes.py}`; `index.html` stays in `src/findata/web/`
- Delete: `src/findata/api/` (all of it), update `tests/test_api.py` to `tests/test_web.py`
- Modify: `src/findata/cli/__init__.py` (`serve` becomes a group: `serve web`; the old `serve` command is removed)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `DaemonClient` (role `web`), `privfs.load_or_create_api_token`, `ClientSettings`.
- Produces: `create_web_app(client: DaemonClient, api_token: str) -> FastAPI` serving `/`, `/favicon.ico`, `/api/health`, `/api/items`, `/api/accounts`, `/api/transactions`, `/api/sync`, `/api/link/token`, `/api/link/exchange`, `/docs`, with the same auth header, Host allow-list and response shapes as today. `findata serve web` runs it on `findata_host:findata_port`.

- [ ] **Step 1: Write failing tests:** port every case in the current `tests/test_api.py` to run against a web app whose client talks to an in-process daemon (same assertions: 401 without token, Host spoof 400, filters, sync result shape, 502 for Plaid errors); add `test_web_returns_503_with_start_hint_when_daemon_is_down` and `test_web_app_never_imports_core_modules` (assert none of the core modules are in `sys.modules` after importing `findata.web.app` in a fresh subprocess).
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

- [ ] **Step 1: Write failing tests:** `test_web_grant_round_trip`, `test_web_lists_and_approves_pending_removals`, `test_web_grant_requires_the_browser_token`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -v`; expected FAIL.
- [ ] **Step 3: Implement** the routes and the UI section, matching the existing page's plain style.
- [ ] **Step 4: Run** `uv run pytest -q`, then check the panel in the browser; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add agent-access panel to the web UI"`

### Task 12: `start --background`

**Files:**
- Modify: `src/findata/daemon/server.py`, `src/findata/cli/commands/daemon.py`
- Test: `tests/test_daemon_background.py`

**Interfaces:**
- Produces: `findata start --background` spawns the daemon detached (new session, stdout/stderr to `Locations.log`), waits until `/v1/status` answers, then exits 0 printing the pid and log path; if the child exits first or the wait times out it prints the reason (last log lines) and exits 1.

- [ ] **Step 1: Write failing tests:** `test_background_start_returns_when_the_daemon_is_ready`, `test_background_start_fails_loudly_if_the_child_dies` (force a schema-behind database; exit 1, message includes `findata migrate`, no stray process), `test_background_daemon_logs_to_file_and_not_the_terminal`, `test_status_and_stop_work_on_a_backgrounded_daemon`, `test_log_file_is_owner_only_and_contains_no_credentials`.
- [ ] **Step 2: Run** `uv run pytest tests/test_daemon_background.py -v`; expected FAIL.
- [ ] **Step 3: Implement** with `subprocess.Popen(..., start_new_session=True)` re-invoking `python -m findata start`; readiness check polls the client with a timeout.
- [ ] **Step 4: Run** `uv run pytest -q`; expected PASS.
- [ ] **Step 5: Commit** `git commit -m "Add start --background"`

### Task 13: Boundary test, end-to-end test, docs

**Files:**
- Create: `tests/test_boundary.py`, `tests/test_daemon_e2e.py`, `docs/adr/0004-daemon-centred-architecture.md`
- Modify: `README.md` (quickstart: `findata start`, `serve web`, `serve mcp`, grant flow), `docs/DEV_SETUP.txt`, roadmap

**Interfaces:** none new.

- [ ] **Step 1: Write the tests:** `test_boundary.py::test_front_ends_and_data_cli_never_import_core_modules` parses every file under `findata/web`, `findata/mcp`, `findata/client` and `findata/cli/commands/{sync,txns,items,sandbox_link,grants,audit}.py` (`commands/daemon.py` is excluded because `start` runs the core; keep `stop` and `status` there using only `DaemonClient`)` and fails on any import of `findata.db`, `findata.security`, `findata.plaid_gateway`, `findata.sync`, `findata.linking`, `findata.waiting`, `findata.queries`, `findata.schema`, `findata.runtime`, `findata.contexts`, `findata.config`; `test_daemon_e2e.py::test_real_socket_end_to_end` starts a daemon on a temp data dir with the fake gateway, drives it with `DaemonClient` as `cli`, `web`, `mcp`, and asserts: read works for all, `mcp` write is refused, works after `grant`, refused after `revoke`, and a wrong credential is rejected.
- [ ] **Step 2: Run** `uv run pytest tests/test_boundary.py tests/test_daemon_e2e.py -v`; expected PASS (they should already hold; if the boundary test fails, fix the import it names).
- [ ] **Step 3: Write the docs:** ADR 0004 recording the decisions in the spec; README quickstart updated; DEV_SETUP describes the process model, credentials, grants, and how to run daemon plus front ends in separate terminals.
- [ ] **Step 4: Run** `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; expected all pass.
- [ ] **Step 5: Commit** `git commit -m "Enforce the daemon boundary; end-to-end test; ADR 0004 and docs"`

---

## Self-review notes

- Spec coverage: command surface (Tasks 5, 8, 9, 10, 12), daemon duties and lock (5), audit (4), roles and credentials (2), grants and user-present write (3, 4, 8, 11), web front end (9), MCP (10), client and `DAEMON_NOT_RUNNING` (6, 8), boundary enforcement (13), scheduler hook (5), foreground default and `--background` (5, 12), Windows fallback (5).
- Calls the plan makes beyond the spec: the grant mechanism (CLI `grant`/`revoke`/`approve` and the web panel) is the spec's marked guess; the `link` operation is grantable but the sandbox-link job is the only agent-triggerable link path; `findata audit` and `GET /v1/audit` exist so the audit log can be read.
