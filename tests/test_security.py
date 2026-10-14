import os
import stat

import pytest
import sqlcipher3
from sqlalchemy import text

from findata import security
from findata.db import make_engine
from findata.security import (
    FieldCipher,
    KeychainUnavailableError,
    Keys,
    load_keys,
    load_or_create_api_token,
    load_or_create_master_key,
)

MARKER = "PLAINTEXT-MARKER-4242"

# Unix permission bits don't exist on Windows; findata relies on the user-profile ACL there.
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


# ---------- field cipher ----------


def test_field_cipher_roundtrip_and_fresh_nonce():
    c = FieldCipher(b"k" * 32)
    a, b = c.encrypt("access-sandbox-xyz", "ctx"), c.encrypt("access-sandbox-xyz", "ctx")
    assert a != b  # random nonce every call
    assert "access-sandbox-xyz" not in a
    assert c.decrypt(a, "ctx") == c.decrypt(b, "ctx") == "access-sandbox-xyz"


def test_field_cipher_binds_context():
    c = FieldCipher(b"k" * 32)
    ct = c.encrypt("secret", "items.access_token:item_1")
    with pytest.raises(RuntimeError, match="does not match"):
        c.decrypt(ct, "items.access_token:item_2")  # ciphertext moved to another row


def test_field_cipher_rejects_wrong_key_and_tampering():
    ct = FieldCipher(b"a" * 32).encrypt("x", "ctx")
    with pytest.raises(RuntimeError, match="does not match"):
        FieldCipher(b"b" * 32).decrypt(ct, "ctx")
    flipped = ct[:-4] + ("AAAA" if ct[-4:] != "AAAA" else "BBBB")
    with pytest.raises(RuntimeError):
        FieldCipher(b"a" * 32).decrypt(flipped, "ctx")
    with pytest.raises(RuntimeError):
        FieldCipher(b"a" * 32).decrypt("not-base64!!", "ctx")


def test_derived_keys_are_deterministic_and_distinct():
    k1, k2 = Keys.from_master(b"m" * 32), Keys.from_master(b"m" * 32)
    assert k1.db_key == k2.db_key and len(k1.db_key) == 32
    ct = k1.cipher.encrypt("x", "c")
    assert k2.cipher.decrypt(ct, "c") == "x"
    # The SQLCipher key must not double as the field-encryption key.
    with pytest.raises(RuntimeError):
        FieldCipher(k1.db_key).decrypt(ct, "c")


# ---------- master key: file backend ----------


@posix_only
def test_file_backend_creates_owner_only_key_and_is_stable(settings):
    k1 = load_or_create_master_key(settings)
    assert len(k1) == 32
    assert stat.S_IMODE(settings.master_key_path.stat().st_mode) == 0o600
    assert load_or_create_master_key(settings) == k1


@posix_only
def test_refuses_world_readable_master_key(settings):
    load_or_create_master_key(settings)
    settings.master_key_path.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        load_or_create_master_key(settings)


def test_refuses_corrupt_master_key(settings):
    load_or_create_master_key(settings)
    settings.master_key_path.write_text("not hex")
    with pytest.raises(RuntimeError, match="corrupt"):
        load_or_create_master_key(settings)


def test_never_mints_new_key_for_existing_database(settings):
    keys = load_keys(settings)
    make_engine(settings.db_path, keys.db_key)
    settings.master_key_path.unlink()
    with pytest.raises(RuntimeError, match="master key was not found"):
        load_or_create_master_key(settings)
    assert not settings.master_key_path.exists()


# ---------- master key: keychain backend ----------


@pytest.fixture
def fake_keychain(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(security, "_keyring_get", lambda account: store.get(account))
    monkeypatch.setattr(
        security, "_keyring_set", lambda account, value: store.update({account: value})
    )
    return store


def test_auto_prefers_keychain_and_writes_no_file(settings, fake_keychain):
    s = settings.model_copy(update={"findata_key_backend": "auto"})
    k = load_or_create_master_key(s)
    assert list(fake_keychain.values()) == [k.hex()]
    assert not s.master_key_path.exists()
    assert load_or_create_master_key(s) == k


def test_keychain_entries_are_per_data_dir(settings, tmp_path, fake_keychain):
    a = settings.model_copy(update={"findata_key_backend": "keychain"})
    b = a.model_copy(update={"findata_data_dir": tmp_path / "other"})
    assert load_or_create_master_key(a) != load_or_create_master_key(b)
    assert len(fake_keychain) == 2


def _no_keychain(monkeypatch):
    def boom(*_a):
        raise KeychainUnavailableError("no OS keychain is available")

    monkeypatch.setattr(security, "_keyring_get", boom)
    monkeypatch.setattr(security, "_keyring_set", boom)


@posix_only
def test_auto_falls_back_to_file_without_keychain(settings, monkeypatch, caplog):
    _no_keychain(monkeypatch)
    s = settings.model_copy(update={"findata_key_backend": "auto"})
    k = load_or_create_master_key(s)
    assert stat.S_IMODE(s.master_key_path.stat().st_mode) == 0o600
    assert "No OS keychain available" in caplog.text
    assert load_or_create_master_key(s) == k


def test_keychain_backend_fails_loudly_without_keychain(settings, monkeypatch):
    _no_keychain(monkeypatch)
    s = settings.model_copy(update={"findata_key_backend": "keychain"})
    with pytest.raises(RuntimeError, match="findata_key_backend=keychain"):
        load_or_create_master_key(s)
    assert not s.master_key_path.exists()


# ---------- whole-database encryption ----------


def test_api_token_is_stable(tmp_path):
    p = tmp_path / "api.token"
    assert load_or_create_api_token(p) == load_or_create_api_token(p)
    assert len(load_or_create_api_token(p)) >= 40


def test_database_file_is_encrypted_on_disk(tmp_path):
    db = tmp_path / "d" / "findata.db"
    engine = make_engine(db, b"k" * 32)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO items (id, access_token_enc, created_at) "
                "VALUES (:id, 'x', CURRENT_TIMESTAMP)"
            ),
            {"id": MARKER},
        )
    engine.dispose()
    for f in db.parent.iterdir():  # main file and any -wal/-shm sidecars
        assert MARKER.encode() not in f.read_bytes(), f
        assert not f.read_bytes().startswith(b"SQLite format 3"), f


def test_wrong_key_is_rejected_with_clear_error(tmp_path):
    db = tmp_path / "d" / "findata.db"
    make_engine(db, b"a" * 32).dispose()
    with pytest.raises(RuntimeError, match="Could not unlock"):
        make_engine(db, b"b" * 32)


def test_unencrypted_legacy_database_is_rejected(tmp_path):
    db = tmp_path / "d" / "findata.db"
    db.parent.mkdir(mode=0o700)
    conn = sqlcipher3.connect(str(db))  # no PRAGMA key: a plain, unencrypted SQLite file
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    db.chmod(0o600)
    with pytest.raises(RuntimeError, match="re-created"):
        make_engine(db, b"k" * 32)


def test_data_survives_reopen_with_same_key(tmp_path):
    db = tmp_path / "d" / "findata.db"
    e = make_engine(db, b"k" * 32)
    with e.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO items (id, access_token_enc, created_at) "
                "VALUES ('i1', 'x', CURRENT_TIMESTAMP)"
            )
        )
    e.dispose()
    with make_engine(db, b"k" * 32).connect() as conn:
        assert conn.execute(text("SELECT id FROM items")).scalar_one() == "i1"


# ---------- file and directory permissions ----------


@posix_only
def test_db_file_is_owner_only(tmp_path):
    db = tmp_path / "d" / "findata.db"
    make_engine(db, b"k" * 32)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


@posix_only
def test_db_file_never_exists_with_loose_mode(tmp_path, monkeypatch):
    seen = []
    real = __import__("findata.db", fromlist=["create_engine"]).create_engine

    def spy(url, **kw):
        seen.append(stat.S_IMODE((tmp_path / "d" / "findata.db").stat().st_mode))
        return real(url, **kw)

    monkeypatch.setattr("findata.db.create_engine", spy)
    make_engine(tmp_path / "d" / "findata.db", b"k" * 32)
    assert seen == [0o600]  # already owner-only before SQLite first opens it


@posix_only
def test_sidecar_files_are_owner_only(tmp_path):
    db = tmp_path / "d" / "findata.db"
    engine = make_engine(db, b"k" * 32)
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    for f in db.parent.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


@posix_only
def test_refuses_loose_existing_data_dir(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(PermissionError, match="chmod 700"):
        make_engine(d / "findata.db", b"k" * 32)
    with pytest.raises(PermissionError, match="chmod 700"):
        security.load_or_create_secret(d / "x.token", lambda: b"t")


@posix_only
def test_refuses_loose_existing_db_file(tmp_path):
    db = tmp_path / "d" / "findata.db"
    make_engine(db, b"k" * 32).dispose()
    db.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        make_engine(db, b"k" * 32)
