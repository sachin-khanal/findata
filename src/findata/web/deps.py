"""FastAPI dependencies for the web front end: the daemon client and the browser session."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from findata.client import DaemonClient
from findata.web.session import COOKIE_NAME, KEY_HEADER

OPEN_LINK = "Open the link printed by `findata serve web`."


class BrowserSessionRequired(Exception):
    """No valid session cookie and request key; the app answers 401 NO_BROWSER_SESSION."""


def get_client(request: Request) -> DaemonClient:
    return request.app.state.client


ClientDep = Annotated[DaemonClient, Depends(get_client)]


def require_session(
    request: Request,
    key: Annotated[str | None, Header(alias=KEY_HEADER)] = None,
) -> None:
    """Both halves of one session: the cookie and its request key. Either alone is refused."""
    if not request.app.state.sessions.valid(request.cookies.get(COOKIE_NAME), key):
        raise BrowserSessionRequired


auth = [Depends(require_session)]
