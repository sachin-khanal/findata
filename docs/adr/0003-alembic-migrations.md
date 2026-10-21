# ADR 0003: Versioned schema migrations with Alembic

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

The schema was created with `Base.metadata.create_all`, which only creates missing tables. Any
later change (a new column, an index) left existing databases behind, and the only remedy was
deleting the data directory. That is acceptable for Sandbox data and not for a real
transaction history.

## Decision

- Schema changes are Alembic migrations, shipped inside the package
  (`findata/migrations`) and driven only through `findata.schema`. There is no `alembic.ini`
  and no URL: the database is SQLCipher-encrypted, so migrations run on the engine from
  `findata.db.make_engine`.
- `make_engine` only opens and unlocks the database. `findata.schema.ensure_current` runs
  when the app builds its runtime:
  - a brand-new database is created at the latest version;
  - a database from before migrations (tables, no version) is stamped as baseline `0001`,
    which is exactly the old `create_all` schema, and left untouched;
  - a database that is behind is refused with an instruction to run `findata migrate`.
- Migrations are never applied implicitly to existing data. `findata migrate` copies the
  database file (owner-only) before it changes anything. `--check` reports without changing.
- SQLite cannot ALTER most things in place, so migrations use Alembic batch mode.
- No downgrade command: a downgrade can silently drop columns, so recovery is the backup.
- A test compares the migrated schema with the SQLAlchemy models, so a model change without a
  migration fails the suite.

## Consequences

- Existing databases keep their data across upgrades; the cost is one explicit step
  (`findata migrate`) after pulling a version that changes the schema.
- Everything that opens the database (CLI, API, future MCP server) gets the same check for
  free through `build_runtime`.
