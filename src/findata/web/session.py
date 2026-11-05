"""Browser sessions for the web front end: a launch secret, and the sessions it starts.

`serve web` prints a link holding the launch secret in its fragment. The page posts it once and
gets back a session: an HttpOnly cookie (the id) plus a request key that the page keeps in its
own localStorage and sends as a header. Cookies for 127.0.0.1 reach every port, so the cookie
alone proves nothing; localStorage is per origin (per port), so the key does not leave this
server's page. Both halves are random, minted together and compared in constant time.

Nothing here is written to disk or logged, and a new `WebSessions` (every web server run)
starts with no sessions.
"""

from __future__ import annotations

import secrets
import threading

COOKIE_NAME = "findata_session"
KEY_HEADER = "X-Findata-Session"


class WebSessions:
    def __init__(self) -> None:
        self.launch_secret = secrets.token_urlsafe(32)
        self._sessions: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def launch_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/#launch={self.launch_secret}"

    def start(self, secret: str) -> tuple[str, str] | None:
        """A new (session id, request key) if `secret` is the launch secret, else None."""
        if not secrets.compare_digest(secret.encode(), self.launch_secret.encode()):
            return None
        pair = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        with self._lock:
            self._sessions.append(pair)
        return pair

    def valid(self, session_id: str | None, request_key: str | None) -> bool:
        """True only for an id and key minted together. Every session is compared, with no
        early exit, so the time taken does not say which part matched."""
        if not session_id or not request_key:
            return False
        sid, key = session_id.encode(), request_key.encode()
        with self._lock:
            sessions = list(self._sessions)
        found = False
        for stored_id, stored_key in sessions:
            both = secrets.compare_digest(sid, stored_id.encode()) & secrets.compare_digest(
                key, stored_key.encode()
            )
            found = found or both
        return found
