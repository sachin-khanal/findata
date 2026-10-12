# findata

A local-first personal finance data layer on [Plaid](https://plaid.com), built to be used by
both humans and AI agents. You run it on your own machine with your own Plaid developer keys.
Your access tokens and transaction data stay on your machine and only ever go to Plaid.

> **Status:** early. Plaid Link, encrypted token storage and incremental transaction sync
> work against Plaid Sandbox. An agent (MCP) interface and ML features are next.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you if needed).

```bash
uv sync
cp .env.example .env         # add PLAID_CLIENT_ID and PLAID_SECRET (Sandbox)
uv run findata init          # creates ~/.findata (key, API token, DB), all owner-only
uv run findata sandbox-link  # links First Platypus Bank test data, no browser needed
uv run findata txns          # print recent transactions
uv run findata serve         # web UI + API at http://127.0.0.1:8765
```

Get Sandbox keys at <https://dashboard.plaid.com/developers/keys>.

## How it works

```
Plaid ──/transactions/sync──▶ sync.py ──▶ SQLite (~/.findata/findata.db)
  ▲                                            │
  └── plaid_gateway.py (only file that         ▼
      imports plaid-python)             api.py (FastAPI, 127.0.0.1 only)
                                              │
                                     web UI · CLI · (next) MCP server
```

- **Sync** follows Plaid's cursor pattern: page until `has_more` is false, restart from the
  original cursor on `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, then apply the whole delta
  and save the new cursor in one DB transaction.
- **Encryption at rest:** the whole database is encrypted with SQLCipher, and Plaid access
  tokens and raw payloads are additionally AES-256-GCM encrypted per field. Both keys derive
  from one master key kept in the OS keychain (owner-only file fallback on headless machines).
  See [ADR 0002](docs/adr/0002-layered-encryption-at-rest.md).
- **Local API** needs an `X-Findata-Token` header (from `~/.findata/api.token`). It only
  accepts `localhost`/`127.0.0.1` Host headers, which blocks DNS-rebinding and cross-site requests.
- **Money** is stored and returned as exact decimal strings. Plaid's sign convention applies:
  positive = money out, negative = money in.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | no auth |
| POST | `/api/link/token` | create a Plaid Link token |
| POST | `/api/link/exchange` | `{"public_token": ...}` → stores Item |
| POST | `/api/sync` | sync all Items |
| GET | `/api/items`, `/api/accounts` | |
| GET | `/api/transactions` | `start`, `end`, `account_id`, `category`, `limit`, `offset` |

Interactive docs: <http://127.0.0.1:8765/docs>.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uvx pre-commit install       # optional: ruff + private-key detection on commit
```

Tests use a fake Plaid gateway, so they need no keys or network access.

## Roadmap

- [ ] Read-only MCP server over the same query layer (scoped tools, audit log)
- [ ] Alembic migrations (the schema is currently created with `create_all`)
- [ ] Dockerfile + reproducible dev container
- [ ] Webhooks / scheduled sync, balance refresh
- [ ] ML: transaction categorization → anomaly detection → cash-flow forecast, with
      model registry, eval-gated promotion and drift monitoring

## License

MIT
