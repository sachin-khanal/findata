# findata

A local-first personal finance data layer on [Plaid](https://plaid.com), built to be used by
both humans and AI agents. You run it on your own machine with your own Plaid developer keys.
Your access tokens and transaction data stay on your machine and only ever go to Plaid.

> **Status:** early. Plaid Link, encrypted token storage, incremental transaction sync, a web
> UI and an agent (MCP) interface work against Plaid Sandbox. ML features are next.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you if needed).

```bash
uv sync
cp .env.example .env         # add PLAID_CLIENT_ID and PLAID_SECRET (Sandbox)
uv run findata init          # creates ~/.findata (key, database, credentials), all owner-only
uv run findata start         # run the daemon in this terminal (Ctrl+C stops it)
```

Get Sandbox keys at <https://dashboard.plaid.com/developers/keys>.

The daemon is the one program that holds your keys and your data. Everything else asks it.
Leave it running and open a second terminal. To run it without holding a terminal, use
`uv run findata start --background` (macOS and Linux). It logs to `~/.findata/daemon.log`, and
`findata stop` stops it either way.

In the second terminal:

```bash
uv run findata sandbox-link  # links First Platypus Bank test data, no browser needed
uv run findata txns          # print recent transactions
uv run findata items list    # linked banks; `items rm <id>` revokes at Plaid + deletes locally
uv run findata status        # the daemon's version, Plaid environment and uptime
uv run findata serve web     # web UI; prints a link to open (add --open to open it for you)
uv run findata serve mcp     # MCP server on stdio, for an AI agent (see below)
```

If the daemon is not running, these commands stop with `DAEMON_NOT_RUNNING` and tell you to run
`findata start`. After pulling updates, stop the daemon and run `uv run findata migrate`. It
refuses to run while the daemon is up, and the daemon cannot start while it runs.

### Connect an agent

`findata serve mcp` speaks MCP over stdio, so your agent host starts it. Give the host this
server config (the exact file depends on the host):

```json
{"command": "findata", "args": ["serve", "mcp"]}
```

With `uv run`, use `{"command": "uv", "args": ["run", "--directory", "<path to this repo>",
"findata", "serve", "mcp"]}`. The daemon must be running. The agent can read items, accounts and
transactions straight away. It cannot change anything until you allow it.

## Agent access

The agent connects with its own credential and the role `mcp`, which is read-only. Writes (the
`sync` tool and the `remove_item` tool) are off until you turn them on, for a limited time.
`remove_item` needs `--ops items.remove` and, as below, an approval for each removal.

Turn them on from a terminal:

```bash
findata grant mcp-write --ops sync --for 15m     # --ops: sync, link, items.remove (comma-separated)
findata revoke mcp-write                         # end it now
findata approve <id>                             # allow one removal the agent asked for
findata audit                                    # who asked for what, and whether it went through
```

- `--ops` defaults to `sync` and `--for` to 15 minutes. The longest grant is 24 hours.
- A grant ends by itself when it expires, and when the daemon stops.
- Removing an Item always needs its own approval, even inside a grant. The agent asks, is given a
  request id, and you run `findata approve <id>`. Then the agent asks again with that id.
- The web UI has an "Agent access" panel that does the same: grant, revoke and approve.

**Touch ID.** On a Mac, the daemon asks for Touch ID (or your password) before it creates a grant,
approves a request, or removes an Item for you. Read the prompt. It says what you are allowing,
and a touch without reading approves it. Where the check is not available (Linux and Windows
today), grants and approvals from the web UI are refused. Do them from a terminal. There, an
agent that can run shell commands as you could also grant itself writes, so run such agents
sandboxed (see ADR 0004, Residual limits).

**What the agent sees when refused.** A write without a grant fails with `WRITE_NOT_GRANTED` and
a message that tells it to ask you to run `findata grant mcp-write --ops <operation>`. Nothing is
queued and it does not retry. Every request is written to the audit log.

**What this does not cover.** It stops an agent from using findata to act as you. It does not stop
an agent that can edit findata's code, stop the daemon and run its own, or read your keychain.
Run such agents in a sandbox that cannot see `~/.findata` or this repo. Details are in
[ADR 0004](docs/adr/0004-daemon-centred-architecture.md).

If you used an earlier version, an `api.token` file may be left in `~/.findata`. Nothing reads it
now, and you can delete it.

## How it works

```
Plaid ◀──────────────▶ daemon (findata start) ──▶ SQLite (~/.findata/findata.db)
                          │   holds the keys, the database, Plaid access,
                          │   grants and the audit log
              owner-only Unix socket (~/.findata/daemon.sock)
                          │
          ┌───────────────┼────────────────┐
     findata CLI    serve web (browser)   serve mcp (agent)
```

- **One daemon, thin front ends.** Only the daemon opens the database or holds the master key.
  The CLI, the web server and the MCP server reach data only through the daemon's socket
  (`DaemonClient`), and a test fails if they import the database, key or Plaid code.
- **Roles.** `cli` and `web` act for you. `mcp` is the agent. Each has its own credential file in
  `~/.findata/credentials/`, readable only by you.
- **Events, not polling.** The daemon pushes events (a sync finished, a grant expired) over a
  stream, so nothing checks on a timer. After a bank is linked, the daemon checks Plaid every 5
  seconds for up to 2 minutes until the history has loaded; Plaid only pushes through webhooks,
  which need a public address.
- **Sync** follows Plaid's cursor pattern: page until `has_more` is false, restart from the
  original cursor on `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, then apply the whole delta
  and save the new cursor in one DB transaction.
- **Encryption at rest:** the whole database is encrypted with SQLCipher, and Plaid access
  tokens and raw payloads are additionally AES-256-GCM encrypted per field. Both keys derive
  from one master key kept in the OS keychain (owner-only file fallback on headless machines).
  See [ADR 0002](docs/adr/0002-layered-encryption-at-rest.md).
- **Web UI.** `findata serve web` prints a launch link such as `http://127.0.0.1:8765/#launch=...`.
  Opening it starts a browser session (an HttpOnly cookie plus a per-session key kept in the
  page's storage; nothing on disk). The page itself holds no credential. Restarting
  `serve web` ends all sessions. The server accepts only `localhost`/`127.0.0.1` Host headers.
- **Money** is stored and returned as exact decimal strings. Plaid's sign convention applies:
  positive = money out, negative = money in.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uvx pre-commit install       # optional: ruff + private-key detection on commit
```

Tests use a fake Plaid gateway, so they need no keys or network access.

## Roadmap

- [x] Alembic migrations (`findata migrate`, backup before changing an existing DB)
- [x] Daemon that owns the data, with a web UI and an MCP server as front ends
- [ ] Scheduled sync and webhooks, balance refresh (next; they run inside the daemon)
- [ ] Webhook relay so Plaid can reach the daemon. This is a separate decision, because it sends
      Item metadata to a second party.
- [ ] `start --background` on Windows
- [ ] Dockerfile + reproducible dev container
- [ ] ML: transaction categorization → anomaly detection → cash-flow forecast, with
      model registry, eval-gated promotion and drift monitoring

## License

MIT
