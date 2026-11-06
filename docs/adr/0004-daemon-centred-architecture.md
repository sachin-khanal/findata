# ADR 0004: A daemon owns the data; everything else is a front end

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

findata is used by people and by AI agents. Before this change, `findata serve` was the web
server, the API, the key holder and the Plaid client in one process, and the CLI opened the
database itself. Every interface held the master key, two processes could sync the same Item at
once, and a planned MCP server would have been a third copy of the same access.

We want one program that holds everything sensitive, and front ends that can be switched on,
replaced or distrusted without widening what they can reach. The full design is in
`docs/superpowers/specs/2026-10-21-daemon-architecture-design.md`. This ADR records the
decisions and the limits.

## Decision

### One daemon, many clients

- `findata start` runs the daemon (`--background` detaches it). It is the only process that
  holds the master key, opens the encrypted database and talks to Plaid. One daemon runs per
  data directory, enforced by a lock file.
- The daemon serves a versioned API (`/v1`) on an owner-only Unix socket inside the owner-only
  data directory. It does not listen on TCP. (Windows would use loopback with the same
  credentials; see the limits below.)
- The CLI data commands, `findata serve web` and `findata serve mcp` reach data only through
  `DaemonClient`. If the daemon is not running they fail with `DAEMON_NOT_RUNNING` and say to
  run `findata start`. There is no fallback to opening the database directly.
- `tests/test_boundary.py` enforces this. It fails if anything under `web`, `mcp`, `client` or
  `cli` imports the core modules (`db`, `security`, `plaid_gateway`, `sync`, `linking`,
  `waiting`, `queries`, `schema`, `runtime`, `contexts`, `config`), at any depth, and checks
  that `import findata.cli` loads none of them. Only `cli/commands/daemon.py`, `setup.py` and
  `migrate.py` are exempt; they run or set up the core and import it inside their functions.
  `tests/test_daemon_e2e.py` runs a real daemon on a real socket.

### Roles, grants and approvals

- Each client has a credential file per role in `credentials/<role>.token` (owner-only). The
  roles are `cli` and `web` (human) and `mcp` (agent). The daemon checks the role on every
  route, and audits every request.
- The `mcp` role is read-only. A human can grant it writes for a limited time:
  `findata grant mcp-write --ops sync,link,items.remove --for 15m` (default `sync`, 15 minutes,
  at most 24 hours), or from the web panel. A grant expires by itself, ends at
  `findata revoke mcp-write`, and is cleared when the daemon stops. A grant is checked once,
  when a request is admitted: revoking it does not stop an agent write already in progress,
  which completes and is audited, but the agent's next write is refused.
- Every agent removal also needs a fresh approval for that one removal (`findata approve <id>`
  or the web panel), even inside a grant.
- A refused agent write fails with `WRITE_NOT_GRANTED` and a message that says what the human
  must do. Nothing is queued or retried.

### User presence

A credential file does not stop an agent that can run shell commands as the user: it can read the
`cli` credential or run `findata grant` itself. So the daemon, not the front end, asks for the
user's presence before the operations that carry human authority: creating a grant, approving a
request, and removing an Item as `cli` or `web`.

- On macOS this is Touch ID, or the login password when Touch ID is unavailable. The prompt
  names what is being allowed. A cancelled, failed or timed-out prompt refuses the operation
  with `USER_PRESENCE_REQUIRED` and changes nothing.
- Where there is no presence check (Linux and Windows today), grants and approvals made from the
  web UI are refused and must be done from a terminal. A browser session is weaker than a
  terminal the user is sitting at, so it must not be the only thing between an agent and a
  write. Terminal grants are allowed and the daemon reports presence as unsupported.

### Events, not polling

The daemon pushes events as NDJSON (`GET /v1/events` for the daemon, `GET /v1/jobs/{id}/events`
for one job) instead of clients asking on a timer. An idle stream carries only keep-alive lines.
A grant's expiry is a timer inside the daemon, and a job ends with a signal. The web server
relays the events to the page.

One poll remains: after a bank is linked, the daemon re-syncs on a bounded schedule (every 5
seconds for up to 2 minutes by default) until Plaid has loaded the history. Plaid only pushes
through webhooks, which need a public HTTPS address. Removing the poll needs a webhook relay,
which is a separate decision because it sends Item metadata to a second party.

### Browser sessions from a launch link

`findata serve web` listens on 127.0.0.1, which every local process and every other account on
the machine can reach. A page that carries its own credential gives that credential to anyone
who asks for `/`, so the page carries none.

- The server prints a launch link once it is listening: `http://127.0.0.1:8765/#launch=<secret>`.
  The secret is in the fragment, so it is never sent in a request line or written to a log. The
  page posts it once and removes it from the address bar.
- The response sets an HttpOnly, SameSite=Strict session cookie and returns a per-session key,
  which the page keeps in its own storage and sends as `X-Findata-Session`. Every data route
  needs both.
- A cookie alone is not enough on 127.0.0.1. Browsers send cookies to every port on the host, so
  any other local web server would receive it. Page storage is kept apart per port, so that
  server never gets the key. In the other direction, scripts on the page (Plaid Link's included)
  never see the HttpOnly cookie. Neither half works alone.
- The secrets live in the web server's memory for one run. Restarting `serve web` ends every
  session. The old persistent browser token (`api.token`) is retired.

### Smaller decisions

- `start --background` learns that the daemon is ready from a pipe the child writes to once it
  is listening, not by probing it. It is POSIX-only for now; Windows is on the roadmap.
- `/docs` (Swagger UI) is dropped from the web server: it would run third-party code on the
  page's own origin.
- Every response from the web server sets `Content-Security-Policy: frame-ancestors 'none'` and
  `X-Frame-Options: DENY`. A page on another 127.0.0.1 port is same-site, so it could otherwise
  frame the page together with its cookie and trick clicks.
- Request ids for approvals never start with `-`, so `findata approve <id>` never reads an id as
  an option.

## Residual limits

This design stops an agent from using the CLI, the socket or the web server to act with human
authority. It does not make a hostile process safe.

- **It does not stop an agent that can change findata's code, stop the daemon and run its own,
  or read the keychain.** Such an agent can do anything the daemon can. Agents that are not
  trusted that far must run sandboxed, without access to `~/.findata` or the findata source.
- **Touch ID protects only if the person reads the prompt.** The prompt names what is being
  allowed, but a reflexive touch approves it.
- **`--open` can expose the launch link.** On Linux, or with `$BROWSER` set, the link is passed
  to the browser in its arguments, where other accounts can see it with `ps`. macOS passes it on
  stdin. If that matters, copy the printed link by hand instead.
- **Another local account can overwrite the session cookie**, which logs the user out. It
  cannot read the cookie.
- **Linux and Windows have no presence check yet.** Web grants and approvals are refused there;
  terminal grants are not confirmed by the user. So on those platforms an agent with a shell as
  the user can read `credentials/cli.token` or run `findata grant` and give itself writes:
  such agents must run sandboxed there.
- **Windows is untested.** There the daemon falls back to loopback TCP with the same
  credentials, protected only by file ACLs on the data directory. The port is recorded in
  `daemon.port` when the socket is bound. A held daemon lock (for example during
  `findata migrate`) does not prove a daemon is serving, and a `daemon.port` left by a crash
  could point a client, and its credential, at another program on that port.
- **One loose or unreadable credential file stops that role.** A `credentials/<role>.token`
  readable by other users, or empty or corrupt, makes that front end fail with a clear error
  naming the file: `chmod 600` it, or delete it and run `findata init`.

## Consequences

- No front end can open the database, load the master key or hold Plaid credentials. A
  compromised web server or MCP server can do what its role allows and nothing more.
- Two processes can no longer sync the same Item at once; the daemon serialises Plaid writes.
- The CLI needs a running daemon for everything except `init` and `migrate`. The cost is one
  extra step (`findata start`).
- Scheduled sync and webhooks have a home (the daemon's scheduler) but are not built.
