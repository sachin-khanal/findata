"""Schema versioning: bring a findata database to the schema this code expects.

Wraps Alembic so nothing else needs to know about it. Rules:

* A brand-new (empty) database is created at the latest version, nothing to lose.
* A database created before migrations existed (tables, no version) is adopted as the
  baseline revision and left untouched.
* A database that is behind is never changed implicitly: `ensure_current` refuses, and
  `findata migrate` applies the pending migrations after copying the database file.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from findata.security import create_private_file

BASELINE = "0001"  # the schema created by the old `create_all`


class SchemaOutOfDateError(RuntimeError):
    """The database is behind (or ahead of) the schema this version of findata expects."""


@dataclass(frozen=True)
class UpgradeResult:
    from_revision: str | None
    to_revision: str
    backup: Path | None
    adopted: bool = False  # a pre-migration database was recognised and stamped

    @property
    def changed(self) -> bool:
        return self.from_revision != self.to_revision


def _script_location() -> str:
    return str(files("findata").joinpath("migrations"))


def _config(connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _script_location())
    cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(_config(None)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _has_tables(engine: Engine) -> bool:
    return bool(set(inspect(engine).get_table_names()) - {"alembic_version"})


def _stamp(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        command.stamp(_config(conn), revision)


def _run_upgrade(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(_config(conn), "head")


def backup_database(engine: Engine) -> Path | None:
    """Copy the database file next to itself (owner-only). None for in-memory databases."""
    name = engine.url.database
    if not name or name == ":memory:":
        return None
    src = Path(name)
    with engine.connect() as conn:  # fold the WAL into the main file so the copy is complete
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = src.with_name(f"{src.name}.bak-{stamp}")
    create_private_file(dest)
    shutil.copyfile(src, dest)
    return dest


def upgrade(engine: Engine, *, backup: bool = True) -> UpgradeResult:
    """Apply pending migrations. An existing database is copied first (`backup`)."""
    head = head_revision()
    existing = _has_tables(engine)
    before = current_revision(engine)
    adopted = before is None and existing
    if adopted:  # created before migrations existed
        _stamp(engine, BASELINE)
        before = BASELINE
    if before == head:
        return UpgradeResult(before, head, None, adopted)
    saved = backup_database(engine) if (backup and existing) else None
    _run_upgrade(engine)
    return UpgradeResult(before, head, saved, adopted)


def ensure_current(engine: Engine) -> None:
    """Create a brand-new database, adopt a pre-migration one, or refuse one that is behind."""
    if not _has_tables(engine) and current_revision(engine) is None:
        _run_upgrade(engine)
        return
    if current_revision(engine) is None:
        _stamp(engine, BASELINE)
    now, head = current_revision(engine), head_revision()
    if now != head:
        raise SchemaOutOfDateError(
            f"The database schema is at version {now} but this findata needs {head}. "
            "Run `findata migrate` (it copies the database file first)."
        )


def schema_matches_models(engine: Engine) -> list:
    """Differences between the migrated schema and the SQLAlchemy models (empty = in sync)."""
    from alembic.autogenerate import compare_metadata

    from findata.db import Base

    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn), Base.metadata)


__all__ = [
    "BASELINE",
    "SchemaOutOfDateError",
    "UpgradeResult",
    "backup_database",
    "current_revision",
    "ensure_current",
    "head_revision",
    "schema_matches_models",
    "upgrade",
]
