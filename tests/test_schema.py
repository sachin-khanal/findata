"""Schema versioning: fresh DBs, pre-migration DBs, pending migrations, backups."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from typer.testing import CliRunner

from findata import cli, schema
from findata.db import Base, make_engine
from findata.schema import (
    BASELINE,
    SchemaOutOfDateError,
    current_revision,
    ensure_current,
    head_revision,
    schema_matches_models,
    upgrade,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")

KEY = b"k" * 32
INSERT_ITEM = text(
    "INSERT INTO items (id, access_token_enc, created_at) VALUES ('i1', 'x', CURRENT_TIMESTAMP)"
)

# A pretend future migration on top of the real head (0002, the audit log).
NEXT_MIGRATION = """
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("items") as batch:
        batch.add_column(sa.Column("nickname", sa.String(64)))


def downgrade():
    with op.batch_alter_table("items") as batch:
        batch.drop_column("nickname")
"""


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "d" / "findata.db"


@pytest.fixture
def with_next_migration(tmp_path, monkeypatch):
    """Point the schema module at a copy of the migrations plus a 0003 that adds a column."""
    src = Path(schema.__file__).parent / "migrations"
    dst = tmp_path / "migrations"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    (dst / "versions" / "0003_nickname.py").write_text(NEXT_MIGRATION)
    monkeypatch.setattr(schema, "_script_location", lambda: str(dst))


def _create_legacy_tables(engine) -> None:
    """The tables the old `create_all` made (the baseline), not today's newer ones."""
    legacy = [Base.metadata.tables[name] for name in ("items", "accounts", "transactions")]
    Base.metadata.create_all(engine, tables=legacy)


def _legacy_db(db: Path):
    """A database as the old code left it: tables from create_all, no version table."""
    engine = make_engine(db, KEY)
    _create_legacy_tables(engine)
    with engine.begin() as conn:
        conn.execute(INSERT_ITEM)
    return engine


def test_fresh_database_is_created_at_head_and_matches_the_models(db):
    engine = make_engine(db, KEY)
    ensure_current(engine)
    assert current_revision(engine) == head_revision()
    assert {"items", "accounts", "transactions", "audit_log"} <= set(
        inspect(engine).get_table_names()
    )
    assert schema_matches_models(engine) == []  # models and migrations have not drifted


def test_pre_migration_database_is_adopted_without_touching_data(db):
    engine = _legacy_db(db)
    assert current_revision(engine) is None
    # adopted as the baseline, then refused: the audit log migration (0002) is pending
    with pytest.raises(SchemaOutOfDateError, match="findata migrate"):
        ensure_current(engine)
    assert current_revision(engine) == BASELINE
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM items")).scalar() == "i1"
    assert "audit_log" not in inspect(engine).get_table_names()
    assert not list(db.parent.glob("*.bak-*"))  # adopting changes nothing, so no backup


def test_upgrade_reports_adoption_of_a_pre_migration_database(db):
    result = upgrade(_legacy_db(db))
    assert result.adopted and result.changed
    assert (result.from_revision, result.to_revision) == (BASELINE, head_revision())
    assert result.backup is not None and result.backup.exists()


def test_migration_0002_creates_audit_log_and_matches_models(db):
    engine = _legacy_db(db)
    result = upgrade(engine)
    assert result.from_revision == BASELINE and current_revision(engine) == head_revision()
    columns = {c["name"]: c for c in inspect(engine).get_columns("audit_log")}
    assert set(columns) == {"id", "at", "role", "operation", "summary", "outcome"}
    assert not any(c["nullable"] for c in columns.values())
    assert schema_matches_models(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM items")).scalar() == "i1"
    with engine.begin() as conn:  # and it can be undone
        command.downgrade(schema._config(conn), BASELINE)
    assert "audit_log" not in inspect(engine).get_table_names()


def test_pending_migration_is_refused_until_migrate_is_run(db, with_next_migration):
    engine = _legacy_db(db)  # at the baseline; the code now expects 0003
    with pytest.raises(SchemaOutOfDateError, match="findata migrate"):
        ensure_current(engine)
    assert current_revision(engine) == BASELINE  # refusing changed nothing
    assert "nickname" not in {c["name"] for c in inspect(engine).get_columns("items")}
    assert "audit_log" not in inspect(engine).get_table_names()


def test_upgrade_applies_migration_keeps_data_and_backs_up_first(db, with_next_migration):
    engine = _legacy_db(db)
    result = upgrade(engine)
    assert (result.from_revision, result.to_revision) == (BASELINE, "0003")
    assert "nickname" in {c["name"] for c in inspect(engine).get_columns("items")}
    assert "audit_log" in inspect(engine).get_table_names()  # 0002 ran on the way
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM items")).scalar() == "i1"
    assert result.backup is not None and result.backup.exists()
    if os.name == "posix":
        assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600
    # the backup is the pre-migration database: still readable with the key, no new column
    old = make_engine(result.backup, KEY)
    assert "nickname" not in {c["name"] for c in inspect(old).get_columns("items")}
    ensure_current(engine)  # now passes


def test_upgrade_is_idempotent_and_makes_no_second_backup(db, with_next_migration):
    engine = _legacy_db(db)
    upgrade(engine)
    again = upgrade(engine)
    assert not again.changed and again.backup is None
    assert len(list(db.parent.glob("*.bak-*"))) == 1


def test_brand_new_database_upgrade_needs_no_backup(db):
    result = upgrade(make_engine(db, KEY))
    assert result.to_revision == head_revision() and result.backup is None


# ---------- CLI ----------


@pytest.fixture
def run(monkeypatch, settings):
    monkeypatch.setattr("findata.cli.commands.migrate.get_settings", lambda: settings)
    return lambda *args: CliRunner().invoke(cli.app, ["migrate", *args])


def test_migrate_creates_then_reports_current(run):
    first = run("--json")
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["event"] == "migrated"
    second = run("--check", "--json")
    assert second.exit_code == 0
    assert json.loads(second.output) == {"event": "current", "revision": head_revision()}


def test_migrate_check_fails_when_behind(run, settings, keys, with_next_migration):
    engine = make_engine(settings.db_path, keys.db_key)
    _create_legacy_tables(engine)  # a pre-migration database; the code now expects 0003
    r = run("--check", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error_code"] == "SCHEMA_OUT_OF_DATE"
    applied = run("--json")
    assert applied.exit_code == 0 and json.loads(applied.output)["to_revision"] == "0003"
    assert json.loads(applied.output)["backup"]


def test_commands_refuse_a_database_that_needs_migrating(settings, keys, monkeypatch):
    engine = make_engine(settings.db_path, keys.db_key)
    _create_legacy_tables(engine)  # adopted as the baseline, so the real 0002 is pending
    monkeypatch.setattr("findata.runtime.get_settings", lambda: settings)
    r = CliRunner().invoke(cli.app, ["txns", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.output)["error_code"] == "SCHEMA_OUT_OF_DATE"
