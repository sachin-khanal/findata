"""Browser sessions: started from the launch link, a cookie plus a per-session request key."""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
from importlib.resources import files
from pathlib import Path

import httpx2
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from findata.web.app import WebServer, create_web_app
from findata.web.session import COOKIE_NAME, KEY_HEADER, WebSessions
from tests import test_web
from tests.test_daemon_events import _eventually
from tests.test_web import launch_secret, login, make_app, posix_only, web_client

d = test_web.d  # the daemon fixtures, shared with the other web tests
gateway = test_web.gateway


@pytest.fixture
def app(d, settings):
    return make_app(d, settings)


def browser(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def test_page_contains_no_credential(app):
    secret = launch_secret(app)
    b = browser(app)
    key = login(b)[KEY_HEADER]  # a session exists; none of it may be in the page
    page = browser(app).get("/")  # a visitor with no cookie at all
    assert page.status_code == 200
    for material in (secret, key, b.cookies.get(COOKIE_NAME)):
        assert material and material not in page.text
    assert "set-cookie" not in page.headers
    assert "__FINDATA_TOKEN__" not in page.text and "X-Findata-Token" not in page.text


def test_launch_secret_starts_an_httponly_samesite_strict_session(app):
    b = browser(app)
    r = b.post("/api/session", json={"launch": launch_secret(app)})
    assert r.status_code == 200 and list(r.json()) == ["request_key"]
    cookie = r.headers["set-cookie"]
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert parts[0].startswith(f"{COOKIE_NAME}=")
    assert "httponly" in parts and "samesite=strict" in parts and "path=/" in parts
    assert not [p for p in parts if p.startswith(("max-age", "expires"))]  # a browser session
    assert r.json()["request_key"] not in cookie  # the key is not in the cookie
    assert b.get("/api/items", headers={KEY_HEADER: r.json()["request_key"]}).status_code == 200


def test_wrong_launch_secret_is_refused(app):
    b = browser(app)
    for secret in ("wrong", "", launch_secret(app)[:-1]):
        r = b.post("/api/session", json={"launch": secret})
        assert r.status_code == 401 and r.json()["error_code"] == "LAUNCH_LINK_INVALID"
        assert "findata serve web" in r.json()["detail"]
        assert "set-cookie" not in r.headers
    assert not b.cookies


OPEN_API_ROUTES = {"/api/health", "/api/session"}


def _api_routes(routes) -> list[APIRoute]:
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):  # an included router, wrapped by FastAPI
            found += _api_routes(route.original_router.routes)
    return found


def data_routes() -> list[tuple[str, str]]:
    """Every /api route the app has, bar the open ones: a new unprotected route fails here."""
    app = create_web_app(None)  # type: ignore[arg-type]  (routes only; never called)
    found = sorted(
        (method, route.path.replace("{request_id}", "r1"))
        for route in _api_routes(app.routes)
        if route.path.startswith("/api") and route.path not in OPEN_API_ROUTES
        for method in route.methods - {"HEAD", "OPTIONS"}
    )
    assert len(found) >= 12 and ("GET", "/api/events") in found  # the walk found the routes
    return found


@pytest.mark.parametrize(("method", "path"), data_routes())
def test_data_routes_need_the_session_cookie_and_its_key(app, method, path):
    mine, other = browser(app), browser(app)
    my_key = login(mine)[KEY_HEADER]
    other_key = login(other)[KEY_HEADER]
    my_cookie = {"Cookie": f"{COOKIE_NAME}={mine.cookies.get(COOKIE_NAME)}"}
    attempts = {
        "no cookie, no key": (browser(app), {}),
        "no cookie, valid key": (browser(app), {KEY_HEADER: my_key}),
        "cookie, no key": (browser(app), my_cookie),
        "cookie, wrong key": (browser(app), {**my_cookie, KEY_HEADER: "wrong"}),
        "cookie with another session's key": (browser(app), {**my_cookie, KEY_HEADER: other_key}),
    }
    for name, (c, headers) in attempts.items():
        r = c.request(method, path, headers=headers, json={})
        assert r.status_code == 401, name
        assert r.json()["error_code"] == "NO_BROWSER_SESSION", name
        assert "findata serve web" in r.json()["detail"], name


def test_open_routes_need_no_session(app):
    b = browser(app)
    assert b.get("/api/health").status_code == 200
    assert b.get("/").status_code == 200
    assert b.get("/favicon.ico").status_code == 204
    for gone in ("/docs", "/redoc", "/openapi.json"):  # no Swagger page from a CDN on this origin
        assert b.get(gone).status_code == 404, gone


def test_session_from_another_server_run_is_refused(d, settings):
    first = make_app(d, settings)
    b = browser(first)
    key = login(b)[KEY_HEADER]
    assert b.get("/api/items", headers={KEY_HEADER: key}).status_code == 200
    restarted = make_app(d, settings)  # a new run: new launch secret, no sessions
    again = browser(restarted)
    again.cookies.set(COOKIE_NAME, b.cookies.get(COOKIE_NAME))
    assert again.get("/api/items", headers={KEY_HEADER: key}).status_code == 401
    old_secret = launch_secret(first)
    assert again.post("/api/session", json={"launch": old_secret}).status_code == 401


def test_old_token_header_is_not_accepted(app):
    from findata.web import deps

    b = browser(app)
    key = login(b)[KEY_HEADER]
    assert not hasattr(deps, "TOKEN_HEADER")
    plain = browser(app)
    assert plain.get("/api/items", headers={"X-Findata-Token": key}).status_code == 401
    only_cookie = browser(app)
    only_cookie.cookies.set(COOKIE_NAME, b.cookies.get(COOKIE_NAME))
    assert only_cookie.get("/api/items", headers={"X-Findata-Token": key}).status_code == 401


def test_sessions_pair_must_be_minted_together():
    s = WebSessions()
    secret = s.launch_url("h", 1).split("#launch=")[1]
    a, b = s.start(secret), s.start(secret)
    assert a and b and a != b and len(s.launch_url("h", 1).split("#launch=")[1]) >= 40
    assert s.valid(*a) and s.valid(*b)
    assert not s.valid(a[0], b[1]) and not s.valid(b[0], a[1])
    assert not s.valid(None, a[1]) and not s.valid(a[0], None) and not s.valid("", "")
    assert s.start("nope") is None
    assert s.launch_url("127.0.0.1", 8765) == f"http://127.0.0.1:8765/#launch={secret}"


@posix_only
def test_launch_secret_never_reaches_the_access_log(d, settings, caplog):
    caplog.set_level("INFO")
    app = create_web_app(web_client(d))
    secret = launch_secret(app)
    folder = Path(tempfile.mkdtemp(dir="/tmp", prefix="fw"))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(folder / "w.sock"))
    sock.listen()
    server = WebServer(app, lifespan="off", log_config=None, log_level="info", access_log=True)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    http = httpx2.Client(
        transport=httpx2.HTTPTransport(uds=str(folder / "w.sock")),
        base_url="http://127.0.0.1",
        timeout=10,
    )
    try:
        # What a browser does with the printed link: the fragment never leaves the browser.
        link = app.state.sessions.launch_url("127.0.0.1", 8765)
        assert http.get(link.replace("http://127.0.0.1:8765", "")).status_code == 200
        r = http.post("/api/session", json={"launch": secret})
        assert r.status_code == 200
        key = r.json()["request_key"]
        assert http.get("/api/items", headers={KEY_HEADER: key}).status_code == 200
        _eventually(
            lambda: sum(r.name == "uvicorn.access" for r in caplog.records) >= 3,
            "the access log lines",
        )
        lines = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
        assert any("POST /api/session" in m for m in lines)
        for secret_value in (secret, key, http.cookies.get(COOKIE_NAME)):
            assert secret_value and secret_value not in caplog.text
    finally:
        server.should_exit = True
        thread.join(10)
        http.close()
        sock.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_serve_web_prints_the_launch_link_and_open_opens_it(settings, monkeypatch):
    import webbrowser

    from typer.testing import CliRunner

    from findata import cli
    from findata.web import app as web_app

    monkeypatch.setenv("FINDATA_DATA_DIR", str(settings.data_dir))
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **kw: opened.append(url) or True)
    built: dict = {}

    class FakeServer:
        def __init__(self, app, on_listening=None, **config):
            built.update(app=app, config=config)
            self.on_listening = on_listening

        def run(self):
            self.on_listening()  # the port is bound

    monkeypatch.setattr(web_app, "WebServer", FakeServer)

    r = CliRunner().invoke(cli.app, ["serve", "web"])
    assert r.exit_code == 0, r.output
    link = built["app"].state.sessions.launch_url("127.0.0.1", 8765)
    assert r.stdout.count(f"Open findata: {link}") == 1 and r.stdout.count("#launch=") == 1
    assert "#launch=" not in r.stderr  # the secret goes to stdout only
    assert opened == []

    r = CliRunner().invoke(cli.app, ["serve", "web", "--open"])
    assert r.exit_code == 0, r.output
    link = built["app"].state.sessions.launch_url("127.0.0.1", 8765)
    assert f"Open findata: {link}" in r.output
    assert opened == [link]


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.5", "example.com"],  # noqa: S104 - hosts that must be refused
)
def test_serve_web_refuses_a_non_loopback_host_before_binding(host, settings, monkeypatch):
    from typer.testing import CliRunner

    from findata import cli
    from findata.web import app as web_app

    monkeypatch.setenv("FINDATA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("FINDATA_HOST", host)
    bound: list = []
    monkeypatch.setattr(web_app, "WebServer", lambda *a, **kw: bound.append(kw))

    r = CliRunner().invoke(cli.app, ["serve", "web"])
    assert r.exit_code == 1
    assert "127.0.0.1" in r.stderr and host in r.stderr
    assert bound == [] and "#launch=" not in r.output


def test_page_removes_the_launch_secret_from_the_address_bar():
    # The tests do not run the page's JavaScript (R40): check what it is written to do.
    html = files("findata.web").joinpath("index.html").read_text()
    assert "location.hash" in html and "launch=" in html
    assert 'history.replaceState(null, "", "/")' in html
    assert "/api/session" in html and "X-Findata-Session" in html
    assert "localStorage" in html and 'credentials: "same-origin"' in html
    assert "NO_BROWSER_SESSION" in html and "LAUNCH_LINK_INVALID" in html
    assert "__FINDATA_TOKEN__" not in html and "X-Findata-Token" not in html
    assert html.index("history.replaceState") < html.index('fetch("/api/session"')


def test_every_response_forbids_framing(app):
    b = browser(app)
    key = login(b)
    responses = [
        b.get("/"),
        b.get("/api/health"),
        b.get("/api/items", headers=key),
        b.get("/api/items"),  # a refusal
        b.get("/nope"),  # a 404
        TestClient(app, base_url="http://evil.test").get("/"),  # a Host refusal
    ]
    for r in responses:
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'", r.url
        assert r.headers["x-frame-options"] == "DENY", r.url


def test_page_handles_a_pasted_link_and_keeps_other_tabs_keys():
    html = files("findata.web").joinpath("index.html").read_text()
    # A link pasted into a tab that is already open is a fragment change, not a reload.
    assert 'addEventListener("hashchange"' in html
    assert html.count("sessionError = null") >= 1 and "startSession().then" in html
    # The key is read from storage on each request; only a still-stored key is dropped.
    assert "const storedKey = () =>" in html and "let sessionKey" not in html
    assert "localStorage.getItem(KEY_STORE) === sent" in html
    # A launch value with stray characters is still removed from the address bar.
    assert html.index("history.replaceState") < html.index("A-Za-z0-9_-]+$/.test")
    assert 'startsWith("#launch=")' in html


def test_plaid_loads_only_when_the_user_clicks_link():
    html = files("findata.web").joinpath("index.html").read_text()
    head = html[: html.index("</head>")]
    assert "cdn.plaid.com" not in head and "<script" not in head
    assert html.count("cdn.plaid.com") == 1 and "await loadPlaid()" in html
    assert html.index("await loadPlaid()") < html.index("Plaid.create")


def test_refused_session_is_shown_in_the_agent_panel_too():
    html = files("findata.web").joinpath("index.html").read_text()
    assert "function sessionRefused" in html
    assert '$("grant-now").textContent = e.message' in html
    assert "if (sessionError) sessionRefused(sessionError)" in html
