from pathlib import Path

from findata.locations import ClientSettings, Locations


def test_locations_are_inside_the_data_dir(tmp_path: Path):
    loc = Locations(tmp_path)
    for name in ("socket", "lock", "pid", "log", "port_file"):
        assert getattr(loc, name).parent == tmp_path
    assert loc.credentials_dir.parent == tmp_path


def test_client_settings_ignore_plaid_secrets(monkeypatch):
    monkeypatch.setenv("PLAID_SECRET", "shh")
    dump = ClientSettings().model_dump()
    assert not [k for k in dump if "plaid" in k.lower() or "secret" in k.lower()]
    assert "shh" not in str(dump)


def test_security_still_exports_the_private_file_helpers():
    from findata import privfs
    from findata.security import ensure_private_dir

    assert ensure_private_dir is privfs.ensure_private_dir
