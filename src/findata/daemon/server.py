"""Running the daemon in the foreground: lock, runtime, credentials, app, socket, serve.

`run_daemon` owns the whole lifetime. The socket is created owner-only inside the owner-only
data directory, a stale one is removed only once the lock proves no live daemon owns it, and
the socket (or Windows port file), pid file and lock are released on every way out: a
shutdown request, Ctrl+C, SIGTERM or an exception.

Uvicorn's access log is off and its own messages are limited to warnings: the audit log is
the record of requests, and nothing here ever logs request headers (they carry credentials).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import uvicorn

from findata.config import Settings
from findata.daemon.app import create_daemon_app
from findata.daemon.audit import AuditLog
from findata.daemon.auth import CredentialStore
from findata.daemon.grants import ApprovalStore, GrantStore
from findata.daemon.lock import DaemonLock
from findata.daemon.scheduler import Scheduler
from findata.locations import Locations
from findata.privfs import create_private_file, ensure_private_dir
from findata.runtime import build_runtime

logger = logging.getLogger(__name__)

SOCKET_MODE = 0o600
# How long a shutdown waits for requests in flight (e.g. a sync) before cancelling them.
GRACEFUL_SHUTDOWN_S = 30


class DaemonStartError(RuntimeError):
    """The daemon cannot start for a reason the user can fix; the message says how."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def use_tcp_fallback(os_name: str = os.name) -> bool:
    """Windows gets loopback TCP (same credentials) instead of a Unix socket."""
    return os_name == "nt"


def _max_socket_path_bytes() -> int:
    # sun_path holds 104 bytes on macOS and the BSDs and 108 on Linux, including the final NUL.
    return 103 if sys.platform == "darwin" or "bsd" in sys.platform else 107


def check_socket_path(path: Path) -> None:
    limit = _max_socket_path_bytes()
    size = len(os.fsencode(path))
    if size > limit:
        raise DaemonStartError(
            f"The daemon socket path {path} is {size} bytes, longer than the {limit}-byte "
            "limit for Unix sockets on this system. Set FINDATA_DATA_DIR to a shorter "
            "directory (the default is ~/.findata)."
        )


def _bind_unix(path: Path) -> socket.socket:
    check_socket_path(path)
    path.unlink(missing_ok=True)  # stale: the caller holds the lock, so no live daemon owns it
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # bind() creates the socket file with mode 0777 & ~umask: this makes it 0600 from the
    # start. The umask is process-wide, so it is changed for the bind call only.
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
    except OSError as exc:
        sock.close()
        raise DaemonStartError(f"Cannot create the daemon socket {path}: {exc}") from exc
    finally:
        os.umask(previous)
    # Enforced again in case a platform ignores the umask for sockets; still before listen(),
    # so nobody can have connected yet.
    os.chmod(path, SOCKET_MODE)
    return sock


def _bind_loopback(port_file: Path) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port_file.unlink(missing_ok=True)
        create_private_file(port_file)
        port_file.write_text(str(sock.getsockname()[1]))
    except BaseException:
        sock.close()
        raise
    return sock


def open_listener(locations: Locations, *, use_tcp: bool | None = None) -> socket.socket:
    """A bound (not yet listening) socket for the daemon. Call only while holding the lock.

    An owner-only Unix socket at `locations.socket`, or on Windows 127.0.0.1 on a free port,
    recorded in the owner-only `locations.port_file`.
    """
    if use_tcp is None:
        use_tcp = use_tcp_fallback()
    ensure_private_dir(locations.data_dir)
    return _bind_loopback(locations.port_file) if use_tcp else _bind_unix(locations.socket)


def configure_logging() -> None:
    """Timestamped log lines on stderr: findata's at INFO, everything else from WARNING."""
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("findata").setLevel(logging.INFO)


@contextlib.contextmanager
def _exit_on_signals(server: uvicorn.Server) -> Iterator[None]:
    """Make SIGINT and SIGTERM a clean shutdown that still runs our cleanup.

    Uvicorn handles both while it serves, then re-raises the signal to whatever handler was
    installed before it; the default SIGTERM handler would end the process right there,
    before the socket and pid file are removed. Signals can only be handled in the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle(signum: int, frame: object) -> None:
        server.should_exit = True

    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _uvicorn_config(app) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        lifespan="off",
        log_config=None,  # keep the process's logging; never uvicorn's default handlers
        log_level="warning",
        access_log=False,  # the audit log records requests (and never their headers)
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


def run_daemon(settings: Settings) -> None:
    """Run the daemon in the foreground until shutdown (`POST /v1/shutdown`, Ctrl+C, SIGTERM).

    Raises before serving, with a message for the user: `DaemonAlreadyRunning`,
    `SchemaOutOfDateError`, `DaemonStartError`, or another RuntimeError/OSError (no master
    key, a data directory others can enter).
    """
    locations = Locations(settings.data_dir)
    use_tcp = use_tcp_fallback()
    if not use_tcp:
        check_socket_path(locations.socket)  # before anything is created
    with DaemonLock(locations.lock, locations.pid):
        try:
            _serve(settings, locations, use_tcp)
        finally:
            locations.socket.unlink(missing_ok=True)
            locations.port_file.unlink(missing_ok=True)
    logger.info("findata daemon stopped.")


def _serve(settings: Settings, locations: Locations, use_tcp: bool) -> None:
    runtime = build_runtime(settings)  # keys, database, schema check
    credentials = CredentialStore(locations)
    credentials.ensure_all()

    def request_exit() -> None:  # POST /v1/shutdown; `server` is assigned below, before serving
        server.should_exit = True

    app = create_daemon_app(
        runtime,
        credentials,
        GrantStore(_utcnow),
        ApprovalStore(_utcnow),
        AuditLog(runtime.session_factory, _utcnow),
        started_at=_utcnow(),
        clock=_utcnow,
        on_shutdown=request_exit,
    )
    server = uvicorn.Server(_uvicorn_config(app))
    listener = open_listener(locations, use_tcp=use_tcp)
    scheduler = Scheduler()
    try:
        scheduler.start()
        where = f"127.0.0.1:{listener.getsockname()[1]}" if use_tcp else str(locations.socket)
        logger.info(
            "findata daemon (%s) listening on %s, pid %d. Press Ctrl+C to stop.",
            settings.plaid_env,
            where,
            os.getpid(),
        )
        with _exit_on_signals(server):
            server.run(sockets=[listener])
    finally:
        scheduler.stop()
        listener.close()
