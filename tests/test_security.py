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
