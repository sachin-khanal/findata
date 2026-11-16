import os
import stat
from pathlib import Path

import pytest

from findata.daemon.auth import ROLE_PERMISSIONS, CredentialStore
from findata.locations import Locations

ROLES = ("web", "cli", "mcp")


def _store(path: Path) -> tuple[CredentialStore, Locations]:
    loc = Locations(path)
    return CredentialStore(loc), loc


def _token(loc: Locations, role: str) -> str:
    return (loc.credentials_dir / f"{role}.token").read_text().strip()


def test_ensure_all_creates_one_owner_only_token_per_role(tmp_path: Path):
    store, loc = _store(tmp_path)
    store.ensure_all()
    store.ensure_all()  # idempotent
    assert sorted(p.name for p in loc.credentials_dir.iterdir()) == [
        f"{r}.token" for r in sorted(ROLES)
    ]
    if os.name == "posix":
        assert stat.S_IMODE(loc.credentials_dir.stat().st_mode) == 0o700
        for r in ROLES:
            assert stat.S_IMODE((loc.credentials_dir / f"{r}.token").stat().st_mode) == 0o600
    assert len({_token(loc, r) for r in ROLES}) == 3


def test_role_for_matches_each_token_and_rejects_others(tmp_path: Path):
    store, loc = _store(tmp_path / "a")
    store.ensure_all()
    other, oloc = _store(tmp_path / "b")
    other.ensure_all()
    for r in ROLES:
        assert store.role_for(_token(loc, r)) == r
    assert store.role_for(None) is None
    assert store.role_for("") is None
    assert store.role_for("short") is None
    assert store.role_for(_token(loc, "web") + "x") is None
    assert store.role_for(_token(oloc, "web")) is None


def test_mcp_role_has_only_read_by_default():
    assert ROLE_PERMISSIONS["mcp"] == frozenset({"read"})
    for r in ("web", "cli"):
        assert ROLE_PERMISSIONS[r] == frozenset({"read", "sync", "link", "items.remove", "admin"})


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_group_readable_credential_file_is_refused(tmp_path: Path):
    store, loc = _store(tmp_path)
    store.ensure_all()
    path = loc.credentials_dir / "mcp.token"
    path.chmod(0o644)
    with pytest.raises(PermissionError, match=r"mcp\.token.*chmod 600"):
        store.role_for("whatever")


def test_empty_credential_file_is_refused(tmp_path: Path):
    store, loc = _store(tmp_path)
    store.ensure_all()
    (loc.credentials_dir / "cli.token").write_text("")
    with pytest.raises(RuntimeError, match=r"cli\.token"):
        store.role_for("whatever")
