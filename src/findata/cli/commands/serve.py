"""`serve web`: run the local web front end, a client of the daemon (role `web`).

Core modules are never imported here: the web server reaches data only through the daemon.
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Run a front end for the daemon.")


@app.command("web")
def web() -> None:
    """Run the local web UI on 127.0.0.1 (needs `findata start` running)."""
    from findata.client import DaemonClient
    from findata.locations import ClientSettings
    from findata.privfs import load_or_create_api_token
    from findata.web.app import WebServer, create_web_app

    s = ClientSettings()
    client = DaemonClient(s.data_dir, "web")
    token = load_or_create_api_token(s.locations.api_token)
    typer.echo(
        f"findata web → http://{s.findata_host}:{s.findata_port} (the daemon must be running)"
    )
    WebServer(
        create_web_app(client, token),
        host=s.findata_host,
        port=s.findata_port,
        timeout_graceful_shutdown=5,  # backstop: open event relays end as shutdown begins
    ).run()


@app.command("mcp")
def mcp() -> None:
    """Run the MCP server on stdio for an agent (role `mcp`; needs `findata start` running).

    Only MCP protocol goes to stdout; nothing else is printed.
    """
    from findata.client import DaemonClient
    from findata.locations import ClientSettings
    from findata.mcp.server import build_mcp_server

    build_mcp_server(DaemonClient(ClientSettings().data_dir, "mcp")).run()
