"""`serve web` and `serve mcp`: front ends that are clients of the daemon (roles `web`, `mcp`).

Core modules are never imported here: front ends reach data only through the daemon.
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Run a front end for the daemon.")


@app.command("web")
def web(
    open_browser: bool = typer.Option(
        False, "--open", help="Open the launch link in the default browser."
    ),
) -> None:
    """Run the local web UI on 127.0.0.1 (needs `findata start` running).

    Prints a launch link; a browser session starts only from that link.
    """
    import webbrowser

    from findata.client import DaemonClient
    from findata.locations import ClientSettings
    from findata.web.app import WebServer, create_web_app
    from findata.web.session import WebSessions

    s = ClientSettings()
    client = DaemonClient(s.data_dir, "web")
    sessions = WebSessions()
    link = sessions.launch_url(s.findata_host, s.findata_port)
    typer.echo(
        f"findata web → http://{s.findata_host}:{s.findata_port} (the daemon must be running)"
    )

    def listening() -> None:
        # Only the terminal sees the launch secret: it is never logged or written to disk.
        typer.echo(f"Open findata: {link}")
        if open_browser:
            webbrowser.open(link)

    WebServer(
        create_web_app(client, sessions),
        on_listening=listening,
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
