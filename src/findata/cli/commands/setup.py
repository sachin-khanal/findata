"""`init` and `serve`: prepare the data directory and run the local web UI."""

from __future__ import annotations

import typer

from findata.config import get_settings
from findata.db import make_engine
from findata.security import load_keys, load_or_create_api_token


def init() -> None:
    """Create the data directory, master encryption key, local API token and database."""
    s = get_settings()
    keys = load_keys(s)
    load_or_create_api_token(s.api_token_path)
    make_engine(s.db_path, keys.db_key)
    typer.echo(f"Initialized {s.data_dir}")


def serve() -> None:
    """Run the local web UI + API on 127.0.0.1."""
    import uvicorn

    from findata.api import create_app

    s = get_settings()
    typer.echo(f"findata ({s.plaid_env}) → http://{s.findata_host}:{s.findata_port}")
    uvicorn.run(create_app(s), host=s.findata_host, port=s.findata_port)
