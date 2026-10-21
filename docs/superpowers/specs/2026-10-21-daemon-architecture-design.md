# findata: daemon-centred architecture

- **Status:** Draft for approval
- **Date:** 2026-10-21

## Intent

findata is used by both people and AI agents, so one headless program must own everything
sensitive and everything else must be a replaceable front end. Today `findata serve` is the
web server, the API, the key holder and the Plaid client in one process, and the CLI opens the
database on its own. That means every interface holds the master key, two processes can sync
the same Item at once, and the planned MCP server would be a third copy of the same access.

The target: a **daemon** that the user starts and that runs in the background. It is the only
thing that holds the master key, opens the encrypted database, talks to Plaid and decides what
is allowed. The web UI and the MCP server are optional front ends that the user switches on
separately. They get all their data from the daemon and can never reach the database.

## Command surface (decided by the user)

| Command | What it does |
|---|---|
| `findata start` | Run the daemon, the overarching software. Everything else depends on it. |
| `findata serve web` | Start the web UI server. Data comes from the daemon. |
| `findata serve mcp` | Start the agent MCP server. Data comes from the daemon. |

**All data goes through the daemon**, including the CLI. `sync`, `txns`, `items list/rm` and
`sandbox-link` become clients of the daemon and fail clearly when it is not running.
`init` and `migrate` still work with no daemon (they run before it can exist, or while it is
stopped).

## Goals

- One process holds the key, database, Plaid credentials and schedule. No other process, not
  the web server, the MCP server or the CLI, ever opens the database file or sees a secret.
- Web UI and MCP run independently of each other: either, both or neither.
- Humans and agents have different, enforceable permissions.
- Every request to the daemon is attributable (who, what, when) in an audit log.
- A place in the daemon for scheduled sync and webhooks, which are the next roadmap item.

## Non-goals

- Building scheduled sync or webhooks. This project only creates the home for them.
- Remote or multi-user access. Everything stays on one machine.
- A packaged installer or service files (launchd/systemd). Documenting them is fine.
- Changing the existing data model, encryption or Plaid sync logic.

## Anti-goals (a failure even if it technically works)

- A front end that can open the database, load the master key or hold Plaid credentials.
- A silent fallback to direct database access when the daemon is down.
- The daemon listening on a network interface or being reachable by other users.
- Secrets or transaction data in logs, the audit log, or error messages.

## Constraints

- The existing core stays the foundation and is imported only by the daemon (plus `init` and
  `migrate`): `db`, `security`, `plaid_gateway`, `sync`, `linking`, `waiting`, `queries`,
  `schema`, `runtime`.
- Works on macOS, Linux and Windows (ADR 0002). macOS and Linux first; if the chosen local
  transport is not viable on Windows, a loopback fallback with the same authentication is
  acceptable there.
- The existing behaviour users rely on is preserved: `--json` output of the CLI (one event
  per line, same event names and exit codes), the web UI, and the `/api/*` data shapes.
- The daemon refuses to start if the schema is behind (as `ensure_current` does now), and only
  one daemon may run per data directory.
- Tests keep using a fake Plaid gateway and need no network.

## Design

### Daemon (`findata start`)

- Runs in the foreground by default, so it works in a terminal, tmux or under launchd.
  *(My call: see "Calls I made" for background mode.)*
- Takes an exclusive lock in the data directory; a second `start` fails with a clear message.
- Loads keys, opens the database, checks the schema, creates the shared `Runtime`.
- Serves an internal, versioned API (`/v1/...`) over an owner-only Unix socket inside the
  owner-only data directory. Not TCP.
- The API covers what the interfaces need today: status/health, link token and exchange,
  sync, items (list, remove), accounts, transactions with the current filters, the sandbox
  link job. Long operations (sandbox-link waiting for history) are jobs that clients can
  follow, so `sandbox-link --json` keeps emitting its events.
- Writes an audit entry for every request: client role, operation, parameters summary,
  result, time. No transaction data or secrets. *(My call: a table added through migration
  0002.)*
- `findata stop` and `findata status` talk to the daemon. A stop mid-sync must not corrupt
  anything; the cursor is already committed atomically with the data.

### Authentication and roles

- Each client authenticates to the daemon with a credential tied to a role, issued and checked
  by the daemon. Minimum roles: **web** (human: read, sync, link, remove) and **mcp** (agent:
  read-only, scoped tools). The CLI acts as the human role.
- Credentials live in owner-only files in the data directory, like the current API token.
- The daemon enforces the role on every route. A front end cannot widen its own access.

### Web front end (`findata serve web`)

- Serves the existing UI on 127.0.0.1:8765 and keeps the existing browser protections (token
  header, Host allow-list, no CORS).
- Its backend is a thin layer that calls the daemon through a typed client. It holds no keys,
  no database and no Plaid credentials.
- Plaid Link still runs in the browser; the link token and exchange go through the daemon.

### MCP front end (`findata serve mcp`)

- Read-only tools over the same data: items, accounts, transactions (with filters), status.
- Default transport is stdio, because agent hosts launch MCP servers themselves. HTTP can come
  later.
- Connects to the daemon as the **mcp** role. If the daemon is down, tools return a clear
  error.

### Clients and the CLI

- One typed `DaemonClient` is the only way a front end or the CLI reaches data. When the
  daemon is not running it raises one specific error that CLI commands turn into
  `error_code: DAEMON_NOT_RUNNING` and "run `findata start`".

### Code layout (suggested; the builder may adjust)

- `findata/daemon/`: app, routes, auth, audit, lock, scheduler hook.
- `findata/client/`: `DaemonClient` over the Unix socket.
- `findata/web/`, `findata/mcp/`: the two front ends, depending only on `client`.
- `findata/cli/`: commands become clients, except `init`, `migrate`, `start`.
- Existing core modules unchanged, imported only under `daemon/` plus `init`/`migrate`.

### Enforcing the boundary

- A test fails if anything under `web/`, `mcp/`, `client/` or the data CLI commands imports
  `db`, `security`, `plaid_gateway`, `sync`, `linking`, `waiting`, `queries`, `schema` or
  `runtime`. This is what makes "all data goes through the daemon" true rather than
  intended.

## Testing

- The daemon's API is tested in process with the fake Plaid gateway, as the API is today.
- Clients and front ends are tested against an in-process daemon.
- At least one end-to-end test uses a real socket: start a daemon, call it from a client,
  confirm a front end cannot read without its credential, and that the mcp role cannot write.
- Existing tests for sync, security, schema and CLI output keep passing (their imports may
  move).

## Calls I made (change any of these)

- Foreground by default, with an optional `--detach` to run in the background. Writing
  launchd/systemd files is out of scope.
- Agents (mcp role) are read-only and cannot trigger a sync, link a bank or remove one.
- Unix socket only on macOS and Linux; loopback with credential on Windows if needed.
- Audit log is a database table (migration 0002), not a text file.
- The old `findata serve` stops starting a server and becomes the parent of `serve web` and
  `serve mcp`.

## Left to the builder

- The internal API framework and wire format details, how jobs are represented, the exact
  lock mechanism, the MCP library, and how the typed client is generated or written.
