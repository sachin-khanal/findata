"""`init`: prepare the data directory."""

from __future__ import annotations

import typer


def init() -> None:
    """Create the data directory, master encryption key, local API token and database."""
    from findata.config import get_settings
    from findata.daemon.auth import CredentialStore
    from findata.db import make_engine
    from findata.locations import Locations
    from findata.schema import ensure_current
    from findata.security import load_keys, load_or_create_api_token

    s = get_settings()
    keys = load_keys(s)
    load_or_create_api_token(s.api_token_path)
    CredentialStore(Locations(s.data_dir)).ensure_all()
    ensure_current(make_engine(s.db_path, keys.db_key))
    typer.echo(f"Initialized {s.data_dir}")
