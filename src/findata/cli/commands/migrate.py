"""`migrate`: bring the database schema up to date (copying the database file first)."""

from __future__ import annotations

import typer

from findata.cli.output import JsonOpt, emit, fail
from findata.config import get_settings
from findata.db import make_engine
from findata.schema import current_revision, head_revision, upgrade
from findata.security import load_keys


def migrate(
    check: bool = typer.Option(
        False, "--check", help="Only report whether the schema is current; exit 1 if it is not."
    ),
    backup: bool = typer.Option(
        True, "--backup/--no-backup", help="Copy the database file before changing it."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Apply pending schema migrations to the local database.

    A brand-new database is simply created. An existing one is copied to
    `findata.db.bak-<timestamp>` (owner-only) before anything changes.
    """
    s = get_settings()
    try:
        engine = make_engine(s.db_path, load_keys(s).db_key)
    except RuntimeError as exc:
        fail(as_json, str(exc), error_code="NOT_CONFIGURED")
    head = head_revision()
    if check:
        now = current_revision(engine)
        if now == head:
            emit(as_json, f"Schema is current (version {head}).", event="current", revision=head)
            return
        fail(
            as_json,
            f"Schema is at {now or 'no version'}, needs {head}. Run `findata migrate`.",
            error_code="SCHEMA_OUT_OF_DATE",
            revision=now,
            head=head,
        )
    result = upgrade(engine, backup=backup)
    if result.adopted and not result.changed:
        emit(
            as_json,
            f"Adopted the existing database as version {head}. No data was changed.",
            event="adopted",
            revision=head,
        )
        return
    if not result.changed:
        emit(as_json, f"Already up to date (version {head}).", event="current", revision=head)
        return
    emit(
        as_json,
        f"Migrated {result.from_revision or 'new database'} → {result.to_revision}."
        + (f" Backup: {result.backup}" if result.backup else ""),
        event="migrated",
        from_revision=result.from_revision,
        to_revision=result.to_revision,
        backup=str(result.backup) if result.backup else None,
    )
