import stat

import pytest

from findata.db import make_engine
from findata.security import TokenCipher, load_or_create_api_token


def test_cipher_roundtrip_and_key_perms(tmp_path):
    key = tmp_path / "d" / "token.key"
    c = TokenCipher.from_key_file(key)
    ct = c.encrypt("access-sandbox-xyz")
    assert "access-sandbox-xyz" not in ct
    assert c.decrypt(ct) == "access-sandbox-xyz"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # Reloading uses the same key.
    assert TokenCipher.from_key_file(key).decrypt(ct) == "access-sandbox-xyz"


def test_wrong_key_fails_loudly(tmp_path):
    ct = TokenCipher.from_key_file(tmp_path / "a.key").encrypt("x")
    with pytest.raises(RuntimeError, match="does not match"):
        TokenCipher.from_key_file(tmp_path / "b.key").decrypt(ct)


def test_refuses_world_readable_key(tmp_path):
    key = tmp_path / "token.key"
    TokenCipher.from_key_file(key)
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        TokenCipher.from_key_file(key)


def test_api_token_is_stable(tmp_path):
    p = tmp_path / "api.token"
    assert load_or_create_api_token(p) == load_or_create_api_token(p)
    assert len(load_or_create_api_token(p)) >= 40


def test_db_file_is_owner_only(tmp_path):
    db = tmp_path / "d" / "findata.db"
    make_engine(db)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_db_file_never_exists_with_loose_mode(tmp_path, monkeypatch):
    seen = []
    real_create_engine = __import__("findata.db", fromlist=["create_engine"]).create_engine

    def spy(url, **kw):
        seen.append(stat.S_IMODE((tmp_path / "d" / "findata.db").stat().st_mode))
        return real_create_engine(url, **kw)

    monkeypatch.setattr("findata.db.create_engine", spy)
    make_engine(tmp_path / "d" / "findata.db")
    assert seen == [0o600]  # already owner-only before SQLite first opens it


def test_sidecar_files_are_owner_only(tmp_path):
    db = tmp_path / "d" / "findata.db"
    engine = make_engine(db)
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    for f in db.parent.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_refuses_loose_existing_data_dir(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(PermissionError, match="chmod 700"):
        make_engine(d / "findata.db")
    with pytest.raises(PermissionError, match="chmod 700"):
        TokenCipher.from_key_file(d / "token.key")


def test_refuses_loose_existing_db_file(tmp_path):
    db = tmp_path / "d" / "findata.db"
    make_engine(db)
    db.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        make_engine(db)
