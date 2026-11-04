"""User presence: the daemon asks for Touch ID (or the password) before human-authority writes.

The requests here go straight to the daemon app with a role credential, as a process calling
the socket directly would. No test ever shows a real prompt: the daemon gets a `FakePresence`,
`MacPresence` gets a fake LAContext, and the guard in conftest fails a test that reaches the
real one.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import httpx2
import pytest

from findata.client import DaemonClient
from findata.daemon.deps import PLAID_WRITE_LOCK
from findata.daemon.grants import GRANTABLE
from findata.daemon.presence import MacPresence, UnsupportedPresence, default_presence
from tests.conftest import ACCESS_TOKEN, FakeGateway, FakePresence
from tests.test_client import _cred_dir
from tests.test_daemon_api import _audit, _build, _error, _link_and_sync, _pages
from tests.test_daemon_jobs import _finish, _start

DENIED = (
    "Not confirmed at the Mac (Touch ID or password was cancelled or timed out). "
    "Nothing was changed."
)
NOT_POSSIBLE = (
    "This needs confirmation at the Mac, and this session cannot show the Touch ID or password "
    "prompt (for example over SSH). Run it from a logged-in session on the Mac."
)
GRANT_SYNC_15 = "allow the findata agent to sync for 15 minutes"
APPROVE_BANK = "approve the agent's request to remove First Platypus Bank"
REMOVE_BANK = "remove First Platypus Bank and its local data"
GATED = {"grants.create", "approvals.approve", "items.remove"}


@pytest.fixture
def gateway():
    return FakeGateway(_pages())


@pytest.fixture
def presence():
    return FakePresence("confirmed")


@pytest.fixture
def d(settings, keys, gateway, presence):
    return _build(settings, keys, gateway, presence=presence)


def _agent_request(d) -> str:
    """A pending agent request to remove item_1, set up without any gated call."""
    d.grants.grant({"items.remove"}, 600)
    r = d.client.delete("/v1/items/item_1", headers=d.as_["mcp"])
    return _error(r, 403, "CONFIRMATION_REQUIRED")["request_id"]


def _gated_rows(d) -> list[tuple[str, str, str, str]]:
    """(role, operation, summary, outcome) of audited gated requests by humans, newest first."""
    return [
        (r["role"], r["operation"], r["summary"], r["outcome"])
        for r in _audit(d)
        if r["operation"] in GATED and r["role"] in ("cli", "web")
    ]


# ---------- grants ----------


def test_grant_denied_stores_no_grant(d, presence):
    presence.outcome = "denied"
    for role in ("cli", "web"):
        r = d.client.post(
            "/v1/grants", headers=d.as_[role], json={"operations": ["sync"], "ttl_s": 900}
        )
        assert _error(r, 403, "USER_PRESENCE_REQUIRED")["detail"] == DENIED
    assert presence.reasons == [GRANT_SYNC_15, GRANT_SYNC_15]
    assert d.grants.current() is None
    _error(d.client.post("/v1/sync", headers=d.as_["mcp"]), 403, "WRITE_NOT_GRANTED")
    assert _gated_rows(d) == [
        ("web", "grants.create", "POST /v1/grants presence=denied", "denied"),
        ("cli", "grants.create", "POST /v1/grants presence=denied", "denied"),
    ]


def test_grant_confirmed_creates_it_and_the_prompt_names_operations_and_duration(d, presence):
    agent = "allow the findata agent to"
    cases = [
        (["items.remove", "sync"], 900, f"{agent} sync and remove items for 15 minutes"),
        (["link"], 60, f"{agent} link banks for 1 minute"),
        (["sync"], 61, f"{agent} sync for 2 minutes"),  # whole minutes, rounded up
        (["sync"], 1, f"{agent} sync for 1 minute"),
        (sorted(GRANTABLE), 86_400, f"{agent} sync, link banks and remove items for 1440 minutes"),
    ]
    for operations, ttl_s, reason in cases:
        presence.reasons.clear()
        r = d.client.post(
            "/v1/grants", headers=d.as_["cli"], json={"operations": operations, "ttl_s": ttl_s}
        )
        assert r.status_code == 200, r.text
        assert r.json()["operations"] == sorted(operations)
        assert presence.reasons == [reason]
    assert d.grants.current().operations == sorted(GRANTABLE)
    rows = _gated_rows(d)
    assert len(rows) == len(cases)
    assert set(rows) == {("cli", "grants.create", "POST /v1/grants presence=confirmed", "ok")}


# ---------- approvals and removals ----------


def test_approve_denied_leaves_the_request_pending(d, presence):
    _link_and_sync(d)
    rid = _agent_request(d)
    presence.outcome = "denied"
    for role in ("cli", "web"):
        r = d.client.post(f"/v1/approvals/{rid}", headers=d.as_[role])
        assert _error(r, 403, "USER_PRESENCE_REQUIRED")["detail"] == DENIED
    assert presence.reasons == [APPROVE_BANK, APPROVE_BANK]
    assert [p["request_id"] for p in d.approvals.pending()] == [rid]
    # the agent's retry still needs an approval, and nothing was removed
    r = d.client.delete("/v1/items/item_1", headers=d.as_["mcp"], params={"request_id": rid})
    _error(r, 403, "CONFIRMATION_REQUIRED")
    assert len(d.client.get("/v1/items", headers=d.as_["cli"]).json()) == 1
    assert d.gateway.removed_tokens == []
    approve = ("approvals.approve", "POST /v1/approvals/{request_id} presence=denied", "denied")
    assert _gated_rows(d) == [("web", *approve), ("cli", *approve)]


@pytest.mark.parametrize("role", ["cli", "web"])
def test_cli_and_web_removal_denied_keeps_the_item_and_never_calls_plaid(d, presence, role):
    _link_and_sync(d)
    presence.outcome = "denied"
    for params in ({}, {"local_only": True}):
        r = d.client.delete("/v1/items/item_1", headers=d.as_[role], params=params)
        assert _error(r, 403, "USER_PRESENCE_REQUIRED")["detail"] == DENIED
    assert presence.reasons == [REMOVE_BANK, REMOVE_BANK]
    assert d.gateway.removed_tokens == []
    items = d.client.get("/v1/items", headers=d.as_[role]).json()
    assert [(i["id"], i["transactions"]) for i in items] == [("item_1", 2)]  # data kept
    summaries = [s for _, _, s, _ in _gated_rows(d)]
    assert summaries == [
        "DELETE /v1/items/{item_id} query=[local_only] presence=denied",
        "DELETE /v1/items/{item_id} presence=denied",
    ]


def test_agent_removal_with_an_approval_never_prompts(d, presence):
    _link_and_sync(d)
    rid = _agent_request(d)
    assert presence.reasons == []  # the agent's request itself asks nobody
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["cli"]).status_code == 204
    assert presence.reasons == [APPROVE_BANK]
    presence.outcome = "denied"  # a prompt now would refuse the removal
    r = d.client.delete("/v1/items/item_1", headers=d.as_["mcp"], params={"request_id": rid})
    assert r.status_code == 204, r.text
    assert presence.reasons == [APPROVE_BANK]
    assert d.gateway.removed_tokens == [ACCESS_TOKEN]
    newest = _audit(d)[0]
    assert (newest["role"], newest["summary"], newest["outcome"]) == (
        "mcp",
        "DELETE /v1/items/{item_id} query=[request_id]",
        "ok",
    )


def test_refusals_before_the_prompt_never_ask(d, presence):
    """Validation, unknown ids and role checks come first: nobody is asked about a request
    that would be refused anyway."""
    cli, mcp = d.as_["cli"], d.as_["mcp"]
    for bad in (
        {"operations": ["admin"], "ttl_s": 60},
        {"operations": ["sync", "db.drop"], "ttl_s": 60},
        {"operations": ["sync"], "ttl_s": 0},
    ):
        _error(d.client.post("/v1/grants", headers=cli, json=bad), 422, "VALIDATION_ERROR")
    _error(d.client.post("/v1/approvals/nope", headers=cli), 404, "NOT_FOUND")
    _error(d.client.delete("/v1/items/nope", headers=cli), 404, "NOT_FOUND")
    grant = {"operations": ["sync"], "ttl_s": 60}
    _error(d.client.post("/v1/grants", headers=mcp, json=grant), 403, "FORBIDDEN")
    _error(d.client.post("/v1/approvals/nope", headers=mcp), 403, "FORBIDDEN")
    assert presence.reasons == []
    assert d.grants.current() is None


def test_reads_sync_link_jobs_revoke_status_and_shutdown_never_prompt(d, presence):
    presence.outcome = "denied"  # a prompt would also refuse the call
    d.grants.grant({"sync", "link"}, 600)
    calls = [("cli", "POST", "/v1/link/exchange", {"json": {"public_token": "public-sandbox-ok"}})]
    for role in ("cli", "web", "mcp"):
        calls += [
            (role, "GET", "/v1/status", {}),
            (role, "GET", "/v1/items", {}),
            (role, "GET", "/v1/accounts", {}),
            (role, "GET", "/v1/transactions", {}),
            (role, "POST", "/v1/sync", {}),
            (role, "POST", "/v1/link/token", {}),
        ]
    for role in ("cli", "web"):
        calls += [
            (role, "GET", "/v1/grants", {}),
            (role, "GET", "/v1/approvals", {}),
            (role, "GET", "/v1/audit", {}),
            (role, "DELETE", "/v1/grants", {}),
        ]
    for role, method, path, kw in calls:
        r = d.client.request(method, path, headers=d.as_[role], **kw)
        assert r.is_success, (role, method, path, r.text)
    job = _finish(d, _start(d, wait=False))
    assert job["state"] == "done", job

    assert d.client.post("/v1/shutdown", headers=d.as_["cli"]).status_code == 202
    assert d.shutdowns == [True]
    assert presence.reasons == []
    assert not any("presence=" in r["summary"] for r in _audit(d, limit=500))


# ---------- outcomes other than confirmed ----------


def test_not_possible_is_refused_with_the_ssh_hint(d, presence):
    _link_and_sync(d)
    rid = _agent_request(d)
    presence.outcome = "not_possible"
    for method, path, kw in (
        ("POST", "/v1/grants", {"json": {"operations": ["link"], "ttl_s": 60}}),
        ("POST", f"/v1/approvals/{rid}", {}),
        ("DELETE", "/v1/items/item_1", {}),
    ):
        r = d.client.request(method, path, headers=d.as_["cli"], **kw)
        assert _error(r, 403, "USER_PRESENCE_REQUIRED")["detail"] == NOT_POSSIBLE
    assert d.grants.current().operations == ["items.remove"]  # the earlier grant, unchanged
    assert [p["request_id"] for p in d.approvals.pending()] == [rid]
    assert len(d.client.get("/v1/items", headers=d.as_["cli"]).json()) == 1
    assert d.gateway.removed_tokens == []
    rows = _gated_rows(d)
    assert len(rows) == 3
    assert all(s.endswith(" presence=not_possible") and o == "denied" for _, _, s, o in rows)


def test_unsupported_platform_allows_and_audits_presence_unsupported(settings, keys, gateway):
    d = _build(settings, keys, gateway, presence=UnsupportedPresence())
    _link_and_sync(d)
    r = d.client.post(
        "/v1/grants", headers=d.as_["cli"], json={"operations": ["items.remove"], "ttl_s": 60}
    )
    assert r.status_code == 200, r.text
    r = d.client.delete("/v1/items/item_1", headers=d.as_["mcp"])
    rid = _error(r, 403, "CONFIRMATION_REQUIRED")["request_id"]
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["web"]).status_code == 204
    r = d.client.delete("/v1/items/item_1", headers=d.as_["mcp"], params={"request_id": rid})
    assert r.status_code == 204, r.text
    _link_and_sync(d)
    assert d.client.delete("/v1/items/item_1", headers=d.as_["cli"]).status_code == 204
    assert [(role, s, o) for role, _, s, o in _gated_rows(d)] == [
        ("cli", "DELETE /v1/items/{item_id} presence=unsupported", "ok"),
        ("web", "POST /v1/approvals/{request_id} presence=unsupported", "ok"),
        ("cli", "POST /v1/grants presence=unsupported", "ok"),
    ]
    agent = [r["summary"] for r in _audit(d) if r["role"] == "mcp" and r["outcome"] == "ok"]
    assert agent == ["DELETE /v1/items/{item_id} query=[request_id]"]  # never asked
    status = d.client.get("/v1/status", headers=d.as_["mcp"]).json()
    assert status["user_presence"] == "unsupported"


# ---------- ordering and content of the prompt ----------


class LockWatchingPresence(FakePresence):
    """Notes, at every prompt, whether the Plaid write lock is held."""

    def __init__(self) -> None:
        super().__init__("confirmed")
        self.lock_held: list[bool] = []

    def confirm(self, reason: str):
        self.lock_held.append(PLAID_WRITE_LOCK.locked())
        return super().confirm(reason)


def test_prompt_runs_before_the_write_lock_is_taken(settings, keys, gateway):
    presence = LockWatchingPresence()
    d = _build(settings, keys, gateway, presence=presence)
    _link_and_sync(d)
    rid = _agent_request(d)
    assert d.client.post(f"/v1/approvals/{rid}", headers=d.as_["cli"]).status_code == 204
    r = d.client.post(
        "/v1/grants", headers=d.as_["web"], json={"operations": ["sync"], "ttl_s": 60}
    )
    assert r.status_code == 200
    assert d.client.delete("/v1/items/item_1", headers=d.as_["cli"]).status_code == 204
    grant_sync_1 = "allow the findata agent to sync for 1 minute"
    assert presence.reasons == [APPROVE_BANK, grant_sync_1, REMOVE_BANK]
    assert presence.lock_held == [False, False, False]
    assert d.gateway.removed_tokens == [ACCESS_TOKEN]  # the removal itself still ran


def test_prompt_reason_contains_no_credentials_or_tokens(d, presence):
    _link_and_sync(d)
    rid = _agent_request(d)
    d.client.post(
        "/v1/grants", headers=d.as_["cli"], json={"operations": ["items.remove"], "ttl_s": 600}
    )
    d.client.post(f"/v1/approvals/{rid}", headers=d.as_["web"])
    d.client.delete("/v1/items/item_1", headers=d.as_["cli"])
    assert presence.reasons == [
        "allow the findata agent to remove items for 10 minutes",
        APPROVE_BANK,
        REMOVE_BANK,
    ]
    said = "\n".join(presence.reasons)
    forbidden = [
        *d.tokens.values(),
        ACCESS_TOKEN,
        "public-sandbox-ok",
        "item_1",
        "acc_1",
        rid,
        "Txn t1",
        "Starbucks",
        "987.65",
        "FOOD_AND_DRINK",
    ]
    assert [f for f in forbidden if f in said] == []


@pytest.mark.parametrize(
    "checker, kind",
    [(FakePresence(kind="touch_id"), "touch_id"), (UnsupportedPresence(), "unsupported")],
)
def test_status_reports_the_presence_kind(settings, keys, gateway, checker, kind):
    d = _build(settings, keys, gateway, presence=checker)
    for role in ("cli", "web", "mcp"):
        assert d.client.get("/v1/status", headers=d.as_[role]).json()["user_presence"] == kind


# ---------- MacPresence, with a fake LAContext ----------


class FakeContext:
    """Stands in for LAContext. `can` answers canEvaluatePolicy; `success` is what the reply
    gets, on another thread as with the framework (None: the reply never comes)."""

    def __init__(self, *, can: bool = True, success: bool | None = True) -> None:
        self.can = can
        self.success = success
        self.policies: list[int] = []
        self.reasons: list[str] = []
        self.invalidated = False

    def canEvaluatePolicy_error_(self, policy, error):
        self.policies.append(policy)
        return self.can, None if self.can else object()

    def evaluatePolicy_localizedReason_reply_(self, policy, reason, reply):
        self.policies.append(policy)
        self.reasons.append(reason)
        if self.success is not None:
            error = None if self.success else object()
            threading.Thread(target=reply, args=(self.success, error)).start()

    def invalidate(self):
        self.invalidated = True


def _mac(context: FakeContext, timeout_s: float = 5.0) -> MacPresence:
    return MacPresence(timeout_s=timeout_s, context_factory=lambda: context)


def test_mac_presence_confirms_when_the_user_authenticates():
    ctx = FakeContext()
    assert _mac(ctx).confirm(REMOVE_BANK) == "confirmed"
    assert ctx.reasons == [REMOVE_BANK]
    # LAPolicyDeviceOwnerAuthentication: Touch ID, or the login password (biometrics-only is 1)
    assert ctx.policies == [2, 2]
    assert not ctx.invalidated


def test_mac_presence_denies_a_failed_or_cancelled_check():
    assert _mac(FakeContext(success=False)).confirm(REMOVE_BANK) == "denied"


def test_mac_presence_denies_when_the_reply_never_arrives():
    ctx = FakeContext(success=None)
    started = time.monotonic()
    assert _mac(ctx, timeout_s=0.05).confirm(REMOVE_BANK) == "denied"
    assert time.monotonic() - started >= 0.05
    assert ctx.invalidated  # the unanswered prompt is taken down


def test_mac_presence_is_not_possible_when_the_session_cannot_prompt():
    ctx = FakeContext(can=False)
    assert _mac(ctx).confirm(REMOVE_BANK) == "not_possible"
    assert ctx.reasons == []  # nothing was shown


def test_a_real_presence_check_fails_the_test_instead_of_prompting():
    with pytest.raises(pytest.fail.Exception, match="FakePresence"):
        MacPresence().confirm(GRANT_SYNC_15)


def test_default_presence_is_touch_id_on_macos_and_unsupported_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    mac = default_presence()
    assert isinstance(mac, MacPresence) and mac.kind == "touch_id"
    for platform in ("linux", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        other = default_presence()
        assert isinstance(other, UnsupportedPresence) and other.kind == "unsupported"
        assert other.confirm(GRANT_SYNC_15) == "unsupported"


def test_only_a_real_check_loads_local_authentication():
    code = (
        "import sys\n"
        "import findata.daemon.server\n"
        "from findata.daemon.presence import default_presence\n"
        "default_presence()\n"
        "assert 'LocalAuthentication' not in sys.modules\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603
    assert r.returncode == 0, r.stderr


# ---------- the client ----------


def test_client_grant_and_approve_wait_long_enough_for_the_prompt(tmp_path):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen[(request.method, request.url.path)] = request.extensions["timeout"]["read"]
        if (request.method, request.url.path) == ("POST", "/v1/grants"):
            body = {"operations": ["sync"], "expires_at": "2026-11-04T10:00:00Z"}
            return httpx2.Response(200, json=body)
        return httpx2.Response(204)

    http = httpx2.Client(transport=httpx2.MockTransport(handler), base_url="http://daemon")
    c = DaemonClient(_cred_dir(tmp_path), "cli", timeout=4, long_timeout=77, http_client=http)
    c.grant(["sync"], 60)
    c.approve("r1")
    c.revoke()
    assert seen == {
        ("POST", "/v1/grants"): 77,
        ("POST", "/v1/approvals/r1"): 77,
        ("DELETE", "/v1/grants"): 4,  # revoking needs no prompt
    }
