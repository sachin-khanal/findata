# ADR 0001: Local-first storage and secret handling

- **Status:** Accepted. Token encryption and key storage superseded by [ADR 0002](0002-layered-encryption-at-rest.md)
- **Date:** 2026-09-23

## Context

findata handles bank access tokens and transaction history. It is self-hosted by each user with
their own Plaid keys. No server run by the project author ever sees user data. The threats that
matter most on a personal machine are:

1. The DB file gets copied somewhere (backups, cloud sync, a stray `git add`).
2. Other local processes or websites in the user's browser talk to the local API.
3. Other OS users on the same machine read the data directory.

## Decision

- **SQLite** in `~/.findata/`: the directory is `0700` and the DB is `0600`. This means
  zero ops and one file to back up, and it is enough for one user's data.
- **Access tokens are encrypted with Fernet.** The key is in `~/.findata/token.key` (`0600`,
  created with `O_EXCL` so it's never silently overwritten). If the DB leaks without the key,
  the tokens are useless. The app refuses to start if the key file is group- or world-readable.
- **Local API** binds to `127.0.0.1` and needs a random token in a custom header. Because of
  the custom header, browsers must send a CORS preflight, which the app never approves, so
  other websites can't make requests to it. `TrustedHostMiddleware` blocks DNS rebinding.
- **Plaid SDK isolation:** only `plaid_gateway.py` imports `plaid-python`. Everything else uses
  a `PlaidGateway` protocol and plain dicts. That makes the core testable offline and gives
  the planned MCP layer one clean boundary.
- **The raw Plaid payload** is kept on each transaction (`raw` JSON column) for later feature
  engineering. The data never leaves the machine.

## Consequences

- If the key file is lost, every Item has to be re-linked. It should be backed up separately
  from the DB.
- The key and the DB are on the same disk, so this does not protect against someone with full
  access to the user account. Next step: move the key into the OS keychain.
- There are no migrations yet. The schema will change, so Alembic comes before any real use.
